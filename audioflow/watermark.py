"""Assinatura de áudio (marca d'água) do AudioFlow.

Sempre que o download termina (com a opção ativa), o app:
    1. Gera a voz da frase (TTS do Windows/SAPI, espeak-ng ou arquivo customizado);
    2. Aplica efeitos opcionais na voz (eco, reverb, voz grave, estilo rádio);
    3. Mistura a voz na música no ponto escolhido (início/meio/final), com
       volume relativo controlado — tudo via ffmpeg.

O arquivo original só é tocado quando a assinatura está ativa.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading

from .utils import config_dir, get_ffmpeg_path

log = logging.getLogger("audioflow.watermark")

VOICE_CACHE_NAME = "voice_watermark.wav"
CUSTOM_CACHE_NAME = "voice_custom.wav"

# Filtros ffmpeg por efeito (aplicados na voz, antes de misturar).
EFFECTS: dict[str, str] = {
    "echo": "aecho=0.8:0.88:120|80:0.25|0.12",
    "reverb": "aecho=0.7:0.6:60|120|180:0.4|0.3|0.2",
    # Voz de DJ/promo: pitch -4 semitons + lenta + graves + compressor + EQ + reverb leve.
    "deep": (
        "asetrate=44100*0.8,aresample=44100,"
        "bass=g=8:f=110,"
        "acompressor=threshold=0.2:ratio=4:attack=5:release=50,"
        "equalizer=f=200:t=q:w=1:g=5,equalizer=f=4000:t=q:w=1:g=-6,"
        "aecho=0.8:0.7:60:0.2"
    ),
    "radio": "highpass=f=400,lowpass=f=3200,acompressor=threshold=0.2:ratio=4:attack=5:release=50",
}

# Versão da receita da voz (invalida o cache quando o TTS/efeitos mudam).
VOICE_VERSION = "4"
VOICE_CACHE_NAME = f"voice_watermark_{VOICE_VERSION}.wav"

# Pitch-down de masculinização (aprox. -4 semitons), com duração preservada.
_MASCULINE_RATE = "0.82"
_MASCULINE_ATEMPO = "1.2195"

# Cadeia "voice tag profissional" (estilo DJ/promo), aplicada na voz gerada:
# EQ (limpa o lixo) -> graves reforçados -> presença (pronúncia clara) ->
# som escuro -> compressão forte (volume consistente) -> saturação leve ->
# reverb curto -> delay curto -> limitador.
_MASTER_CHAIN = (
    "silenceremove=start_periods=1:start_silence=0.05:start_threshold=-50dB,"
    "areverse,"
    "silenceremove=start_periods=1:start_silence=0.05:start_threshold=-50dB,"
    "areverse,"
    "highpass=f=75,"
    "bass=g=7:f=100,"
    "equalizer=f=2500:t=q:w=1:g=2.5,"
    "lowpass=f=14000,"
    "acompressor=threshold=0.2:ratio=8:attack=5:release=120:makeup=2,"
    "acontrast=1.3,"
    "aecho=0.8:0.7:55:0.12,"
    "aecho=0.8:0.85:100:0.18,"
    "alimiter=limit=0.95"
)

# Codificadores por extensão (mantém qualidade alta na remixagem).
_CODECS: dict[str, list[str]] = {
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
    ".wav": ["-c:a", "pcm_s16le"],
    ".m4a": ["-c:a", "aac", "-b:a", "320k"],
    ".flac": ["-c:a", "flac"],
    ".opus": ["-c:a", "libopus", "-b:a", "256k"],
    ".webm": ["-c:a", "libopus", "-b:a", "256k"],
}

_gen_lock = threading.Lock()


def _ffmpeg_cmd(name: str) -> str | None:
    """Caminho do binário ffmpeg/ffprobe no diretório do ffmpeg (ou no PATH)."""
    ffdir = get_ffmpeg_path()
    if ffdir:
        exe = name + (".exe" if os.name == "nt" else "")
        candidate = os.path.join(ffdir, exe)
        if os.path.exists(candidate):
            return candidate
    return shutil.which(name)


def _sapi_speak(text: str, dest: str) -> bool:
    """Gera a voz com o TTS nativo do Windows (System.Speech).

    Prefere voz masculina (estilo DJ/promo) e ritmo levemente lento.
    Retorna True se a voz masculina foi selecionada (senão, a chamadora
    aplica pitch-down para masculinizar a voz feminina).
    """
    letters = " ".join(text)  # soletra: "L J C D"
    script = (
        "Add-Type -AssemblyName System.Speech\n"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "$s.SetOutputToWaveFile('%s')\n"
        "$pick = @($s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name } | "
        "Where-Object { $_ -match 'Mark|David|Guy|George' } | Select-Object -First 1)\n"
        "$wasMale = $false\n"
        "if ($pick) { $s.SelectVoice($pick); $wasMale = $true }\n"
        "$s.Rate = -1\n"
        "$s.Speak('%s')\n"
        "$s.Dispose()\n"
        "Write-Output ('SAPI_MALE=' + $wasMale)\n"
    ) % (dest.replace("'", "''"), letters.replace("'", "''"))
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".ps1", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(script)
            script_path = fh.name
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) <= 1000:
            return False
        return b"SAPI_MALE=True" in result.stdout or b"SAPI_MALE=True" in result.stderr
    except Exception:
        log.exception("TTS SAPI falhou")
        return False
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def _espeak_speak(text: str, dest: str) -> bool:
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    if not espeak:
        return False
    try:
        result = subprocess.run(
            [espeak, "-w", dest, " ".join(text)],
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0 and os.path.exists(dest)
    except Exception:
        log.exception("espeak falhou")
        return False


def _convert_to_wav(src: str, dest: str) -> bool:
    ffmpeg = _ffmpeg_cmd("ffmpeg")
    if not ffmpeg:
        return False
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", src, "-ar", "44100", "-ac", "1", dest],
            capture_output=True,
            timeout=120,
        )
        return result.returncode == 0 and os.path.exists(dest)
    except Exception:
        log.exception("conversão da voz falhou")
        return False


def _master_voice(dest: str, masculinize: bool) -> None:
    """Aplica a cadeia de voz de tag profissional na voz gerada (in-place).

    ``masculinize``: baixa o pitch (~-4 st) mantendo a duração quando a
    voz do TTS é feminina (não há voz masculina instalada no Windows).
    """
    ffmpeg = _ffmpeg_cmd("ffmpeg")
    if not ffmpeg:
        return
    chain = "aresample=44100"
    if masculinize:
        chain += f",asetrate=44100*{_MASCULINE_RATE},aresample=44100,atempo={_MASCULINE_ATEMPO}"
    chain += "," + _MASTER_CHAIN
    tmp = dest + ".mtmp.wav"
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", dest, "-af", chain, tmp],
            capture_output=True,
            timeout=120,
        )
        if result.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 1000:
            os.replace(tmp, dest)
        else:
            log.error("masterização da voz falhou: %s",
                      result.stderr.decode("utf-8", "replace")[-300:] if result.stderr else "")
    except Exception:
        log.exception("masterização da voz falhou")
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _generate_tts(text: str, dest: str) -> bool:
    male = False
    ok = False
    if os.name == "nt":
        male = _sapi_speak(text, dest)
        ok = os.path.exists(dest) and os.path.getsize(dest) > 1000
        if not ok:
            log.warning("SAPI indisponível; tentando espeak-ng")
    if not ok:
        male = True  # a voz padrão do espeak é masculina
        ok = _espeak_speak(text, dest)
    if ok:
        _master_voice(dest, masculinize=not male)
    return ok


def get_voice_wav(text: str, voice_source: str) -> str | None:
    """Retorna o caminho de um WAV com a voz pronta (gerando se preciso)."""
    voice_source = voice_source or "tts"
    with _gen_lock:
        if voice_source == "tts":
            dest = os.path.join(config_dir(), VOICE_CACHE_NAME)
            if os.path.exists(dest) and os.path.getsize(dest) > 1000:
                return dest
            if _generate_tts(text or "LJCD", dest):
                return dest
            log.error("não foi possível gerar a voz (TTS indisponível)")
            return None

        if not os.path.exists(voice_source):
            log.error("arquivo de voz não encontrado: %s", voice_source)
            return None
        dest = os.path.join(config_dir(), CUSTOM_CACHE_NAME)
        try:
            src_mtime = os.path.getmtime(voice_source)
        except OSError:
            src_mtime = 0
        try:
            dest_mtime = os.path.getmtime(dest)
        except OSError:
            dest_mtime = 0
        if dest_mtime < src_mtime or os.path.getsize(dest or "") <= 1000:
            if not _convert_to_wav(voice_source, dest):
                log.error("não foi possível converter o arquivo de voz")
                return None
        return dest


def _effects_chain(effects: list[str] | None) -> str:
    chain = []
    for name in effects or []:
        filt = EFFECTS.get(str(name))
        if filt:
            chain.append(filt)
    return ",".join(chain)


def _duration_ms(media: str) -> float:
    """Duração em ms, via ffprobe (ou ffmpeg -i como fallback)."""
    ffprobe = _ffmpeg_cmd("ffprobe")
    if ffprobe:
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", media],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return float(result.stdout.strip() or 0) * 1000
        except Exception:
            log.exception("ffprobe falhou em %s", media)
    ffmpeg = _ffmpeg_cmd("ffmpeg")
    if ffmpeg:
        try:
            result = subprocess.run(
                [ffmpeg, "-i", media], capture_output=True, text=True, timeout=30
            )
            import re as _re

            match = _re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr)
            if match:
                h, m, s = match.groups()
                return (int(h) * 3600 + int(m) * 60 + float(s)) * 1000
        except Exception:
            log.exception("ffmpeg -i falhou em %s", media)
    return 0.0


# Volume da música durante a assinatura (ducking estilo rádio/DJ) e rampa
# de transição suave (em segundos). Margem extra no final cobre o tail do eco.
DUCK_VOLUME = 0.4
DUCK_RAMP_S = 0.08
DUCK_TAIL_S = 0.35


def _duck_filter(start_s: float, dur_s: float) -> str:
    """Filtro que abaixa a música enquanto a voz fala (com fades suaves)."""
    duck, r = DUCK_VOLUME, DUCK_RAMP_S
    end = start_s + dur_s
    expr = (
        "if(lt(t\\,{s})\\,1\\,"
        "if(lt(t\\,{sr})\\,1-(1-{d})*(t-{s})/{r}\\,"
        "if(lt(t\\,{e})\\,{d}\\,"
        "if(lt(t\\,{er})\\,{d}+(1-{d})*(t-{e})/{r}\\,1))))"
    ).format(s=start_s, sr=start_s + r, e=end, er=end + r, d=duck, r=r)
    return f"volume='{expr}':eval=frame"


def _delay_ms(position: str, music_ms: float, voice_ms: float) -> int:
    if position == "start":
        return 0
    if position == "end":
        return max(0, int(music_ms - voice_ms))
    return max(0, int((music_ms - voice_ms) / 2))


def apply_watermark(
    music_path: str,
    text: str,
    voice_source: str,
    position: str = "middle",
    volume: float = 0.3,
    effects: list[str] | None = None,
) -> bool:
    """Mistura a assinatura de voz na música (in-place). True em caso de sucesso."""
    ffmpeg = _ffmpeg_cmd("ffmpeg")
    if not ffmpeg:
        log.error("ffmpeg não encontrado; assinatura de áudio indisponível")
        return False
    if not os.path.exists(music_path):
        return False

    voice = get_voice_wav(text, voice_source)
    if not voice:
        return False

    ext = os.path.splitext(music_path)[1].lower()
    codec = _CODECS.get(ext)
    if codec is None:
        log.warning("extensão %s sem codificador definido; pulando assinatura", ext)
        return False

    music_ms = _duration_ms(music_path)
    voice_ms = _duration_ms(voice)
    delay = _delay_ms(position, music_ms, voice_ms)
    vol = max(0.0, min(1.0, float(volume or 0)))
    chain = _effects_chain(effects)
    voice_filter = f"[1:a]{chain}," if chain else "[1:a]"
    voice_filter += f"volume={vol:.2f},adelay={delay}:all=1[voice]"
    duck = _duck_filter(delay / 1000.0, (voice_ms / 1000.0) + DUCK_TAIL_S)

    tmp = music_path + ".wmtmp" + ext
    try:
        cmd = [
            ffmpeg, "-y",
            "-i", music_path,
            "-i", voice,
            "-filter_complex",
            f"{duck}[music];{voice_filter};"
            f"[music][voice]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]",
            "-map", "[aout]",
            *codec,
            tmp,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0:
            log.error("ffmpeg da assinatura falhou: %s", result.stderr.decode("utf-8", "replace")[-500:])
            return False
        if not os.path.exists(tmp):
            return False
        os.replace(tmp, music_path)
        log.info("assinatura aplicada em %s (posição=%s, volume=%.2f, delay=%dms)",
                 os.path.basename(music_path), position, vol, delay)
        return True
    except Exception:
        log.exception("falha ao aplicar a assinatura")
        return False
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def tts_available() -> bool:
    """Verifica se há algum TTS disponível (para avisar a interface)."""
    if os.name == "nt":
        return True
    return bool(shutil.which("espeak-ng") or shutil.which("espeak"))
