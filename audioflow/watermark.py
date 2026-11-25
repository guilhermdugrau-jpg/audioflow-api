"""Assinatura de áudio (marca d'água) do AudioFlow.

Sempre que o download termina (com a opção ativa), o app:
    1. Gera a voz da frase (TTS neural do Edge, espeak-ng ou arquivo customizado);
    2. Aplica efeitos opcionais na voz (eco, reverb, voz grave, estilo rádio);
    3. Mistura a voz na música no ponto escolhido (início/meio/final), com
       volume relativo controlado e "ducking" (a música abaixa enquanto a
       assinatura fala e volta ao normal) — tudo via ffmpeg.

O arquivo original só é tocado quando a assinatura está ativa.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading

from .utils import config_dir, get_ffmpeg_path

log = logging.getLogger("audioflow.watermark")

CUSTOM_CACHE_NAME = "voice_custom.wav"

# Vozes neurais (edge-tts): nome, pitch e velocidade.
# "deep" é o padrão: voz masculina grave, estilo locutor/DJ.
VOICE_PRESETS: dict[str, tuple[str, str, str]] = {
    "deep": ("en-US-ChristopherNeural", "-25Hz", "-8%"),
    "ultra": ("en-US-ChristopherNeural", "-45Hz", "-12%"),
    "pt": ("en-US-AndrewMultilingualNeural", "-30Hz", "-8%"),
}
DEFAULT_VOICE = "deep"
VOICE_LABELS = {
    "deep": "Grave (recomendado)",
    "ultra": "Muito grave",
    "pt": "Português (grave)",
}

# Filtros ffmpeg por efeito (aplicados na voz, antes de misturar).
EFFECTS: dict[str, str] = {
    "echo": "aecho=0.8:0.88:120|80:0.25|0.12",
    "reverb": "aecho=0.7:0.6:60|120|180:0.4|0.3|0.2",
    # Reforço opcional: mais 2 semitons para baixo + corpo.
    "deep": (
        "asetrate=44100*0.9,aresample=44100,atempo=1.1111,"
        "bass=g=7:f=120,equalizer=f=3200:t=q:w=1:g=-3"
    ),
    "radio": "highpass=f=400,lowpass=f=3200,acompressor=threshold=0.2:ratio=4:attack=5:release=50",
}

# Versão da receita da voz (invalida o cache quando o TTS/efeitos mudam).
VOICE_VERSION = "6"

# Pitch-down de masculinização para o fallback espeak (duração preservada).
_ESPEAK_RATE = "0.84"
_ESPEAK_ATEMPO = "1.1905"

# Cadeia "voice tag profissional" (estilo DJ/promo), aplicada na voz gerada:
# limpa silêncio -> EQ (tira lixo, dá corpo) -> compressão forte ->
# presença -> reverb curto -> limitador -> loudness constante.
_MASTER_CHAIN = (
    "silenceremove=start_periods=1:start_silence=0.04:start_threshold=-50dB,"
    "areverse,"
    "silenceremove=start_periods=1:start_silence=0.04:start_threshold=-50dB,"
    "areverse,"
    "highpass=f=60,"
    "bass=g=5:f=110,"
    "equalizer=f=180:t=q:w=1:g=2.5,"
    "equalizer=f=2800:t=q:w=1.2:g=2,"
    "acompressor=threshold=0.15:ratio=6:attack=4:release=110:makeup=2,"
    "aecho=0.9:0.75:70:0.1,"
    "alimiter=limit=0.95,"
    "loudnorm=I=-10:TP=-1.2:LRA=9,"
    "aresample=44100"
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


def _edge_speak(text: str, dest: str, preset: str) -> bool:
    """Gera a voz com o TTS neural do Microsoft Edge (grátis, sem chave).

    É o caminho principal: voz humana de verdade, bem mais encorpada que o
    espeak. Se a rede bloquear, a chamadora cai no espeak-ng.
    """
    voice, pitch, rate = VOICE_PRESETS.get(preset, VOICE_PRESETS[DEFAULT_VOICE])
    raw = dest + ".edge.mp3"
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "edge_tts",
                "--voice", voice,
                "--text", text,
                f"--pitch={pitch}",
                f"--rate={rate}",
                "--write-media", raw,
                "--no-progress",
            ],
            capture_output=True,
            timeout=45,
        )
        if result.returncode != 0 or not os.path.exists(raw) or os.path.getsize(raw) < 2000:
            detail = (result.stderr or b"").decode("utf-8", "replace")[-300:]
            log.warning("edge-tts indisponível: %s", detail.strip())
            return False
        return _convert_to_wav(raw, dest)
    except Exception:
        log.exception("edge-tts falhou")
        return False
    finally:
        try:
            os.unlink(raw)
        except OSError:
            pass


def _espeak_speak(text: str, dest: str) -> bool:
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    if not espeak:
        return False
    try:
        result = subprocess.run(
            [
                espeak,
                "-w", dest,
                "-v", "en-us+m3",   # variante masculina grave
                "-p", "18",          # pitch baixo
                "-s", "138",         # ritmo de locutor
                "-g", "4",
                text,
            ],
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

    ``masculinize``: baixa o pitch mantendo a duração quando a voz do TTS
    não é grave o bastante (fallback espeak/SAPI feminino).
    """
    ffmpeg = _ffmpeg_cmd("ffmpeg")
    if not ffmpeg:
        return
    chain = "aresample=44100"
    if masculinize:
        chain += f",asetrate=44100*{_ESPEAK_RATE},aresample=44100,atempo={_ESPEAK_ATEMPO}"
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


def _generate_tts(text: str, dest: str, preset: str) -> bool:
    # 1) TTS neural (qualidade real de locutor).
    if _edge_speak(text, dest, preset):
        _master_voice(dest, masculinize=False)
        return True

    # 2) Fallback local: espeak-ng (Linux) ou SAPI (Windows).
    male = False
    ok = False
    if os.name == "nt":
        male = _sapi_speak(text, dest)
        ok = os.path.exists(dest) and os.path.getsize(dest) > 1000
        if not ok:
            log.warning("SAPI indisponível; tentando espeak-ng")
    if not ok:
        male = True  # a variante m3 do espeak já é masculina/grave
        ok = _espeak_speak(text, dest)
    if ok:
        _master_voice(dest, masculinize=not male)
    return ok


def _voice_cache_path(text: str, preset: str) -> str:
    key = hashlib.sha1(
        f"{VOICE_VERSION}|{preset}|{text}".encode("utf-8", "replace")
    ).hexdigest()[:12]
    return os.path.join(config_dir(), f"voice_{preset}_{key}.wav")


def get_voice_wav(text: str, voice_source: str) -> str | None:
    """Retorna o caminho de um WAV com a voz pronta (gerando se preciso)."""
    voice_source = voice_source or DEFAULT_VOICE

    # Presets neurais do site (deep/ultra/pt). "tts" antigo vira o padrão.
    preset = voice_source if voice_source in VOICE_PRESETS else DEFAULT_VOICE
    if voice_source in VOICE_PRESETS or voice_source in ("tts", "default"):
        text = (text or "Lucas CDs").strip() or "Lucas CDs"
        with _gen_lock:
            dest = _voice_cache_path(text, preset)
            if os.path.exists(dest) and os.path.getsize(dest) > 1000:
                return dest
            if _generate_tts(text, dest, preset):
                return dest
            log.error("não foi possível gerar a voz (TTS indisponível)")
            return None

    # Arquivo de voz customizado (app desktop).
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


# Ducking: a música abaixa enquanto a voz fala e volta suavemente.
# 0.45 ≈ -7 dB (dá para ouvir a voz com clareza sem sumir com a música).
DUCK_VOLUME = 0.45
DUCK_RAMP_S = 0.09
DUCK_TAIL_S = 0.35


def _duck_filters(delays_ms: list[int], voice_s: float) -> list[str]:
    """Um filtro de volume por ocorrência da voz (fades suaves)."""
    duck, r = DUCK_VOLUME, DUCK_RAMP_S
    dur = voice_s + DUCK_TAIL_S
    filters = []
    for delay in delays_ms:
        s = delay / 1000.0
        e = s + dur
        expr = (
            "if(lt(t\\,{s})\\,1\\,"
            "if(lt(t\\,{sr})\\,1-(1-{d})*(t-{s})/{r}\\,"
            "if(lt(t\\,{e})\\,{d}\\,"
            "if(lt(t\\,{er})\\,{d}+(1-{d})*(t-{e})/{r}\\,1))))"
        ).format(s=s, sr=s + r, e=e, er=e + r, d=duck, r=r)
        filters.append(f"volume='{expr}':eval=frame")
    return filters


def _delay_ms(position: str, music_ms: float, voice_ms: float) -> int:
    if position == "start":
        return 0
    if position == "end":
        return max(0, int(music_ms - voice_ms))
    return max(0, int((music_ms - voice_ms) / 2))


def _voice_gain_db(volume: float) -> float:
    """Converte o slider (0–1) em ganho de dB.

    A voz já sai normalizada por volta de -14 LUFS; aqui o slider ajusta de
    -9 dB (30%) até +6 dB (100%), mantendo a assinatura sempre audível.
    """
    vol = max(0.0, min(1.0, float(volume or 0)))
    if vol <= 0.01:
        return -60.0
    return max(-9.0, min(6.0, 3.0 + (vol - 0.5) * 14.0))


def render_voice_preview(
    text: str,
    voice: str = DEFAULT_VOICE,
    effects: list[str] | None = None,
    volume: float = 0.45,
) -> str | None:
    """Gera um MP3 curto só com a voz (para o botão "Ouvir exemplo")."""
    ffmpeg = _ffmpeg_cmd("ffmpeg")
    if not ffmpeg:
        return None
    voice_path = get_voice_wav(text, voice)
    if not voice_path:
        return None
    chain = _effects_chain(effects)
    gain = _voice_gain_db(volume)
    filters = []
    if chain:
        filters.append(chain)
    filters.append(f"volume={gain:.1f}dB")
    filters.append("alimiter=limit=0.98")
    out = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    out.close()
    try:
        result = subprocess.run(
            [
                ffmpeg, "-y", "-i", voice_path,
                "-af", ",".join(filters),
                "-c:a", "libmp3lame", "-b:a", "192k",
                out.name,
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0 or os.path.getsize(out.name) < 1000:
            log.error("prévia da voz falhou: %s",
                      result.stderr.decode("utf-8", "replace")[-300:])
            os.unlink(out.name)
            return None
        return out.name
    except Exception:
        log.exception("prévia da voz falhou")
        try:
            os.unlink(out.name)
        except OSError:
            pass
        return None


def apply_watermark(
    music_path: str,
    text: str,
    voice_source: str,
    position: str = "middle",
    volume: float = 0.45,
    effects: list[str] | None = None,
    repeat_every: float = 0.0,
    repeat_count: int = 0,
) -> bool:
    """Mistura a assinatura de voz na música (in-place).
    - position: "start", "middle", "end" (usado se repeat_every=0)
    - repeat_every: intervalo em segundos entre repetições (0 = apenas uma vez na position)
    - repeat_count: máximo de repetições (0 = ilimitado até o fim da música)
    """
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
    music_s = music_ms / 1000.0
    voice_s = voice_ms / 1000.0
    vol = max(0.0, min(1.0, float(volume or 0)))
    gain = _voice_gain_db(vol)
    chain = _effects_chain(effects)
    voice_base_filter = f"[1:a]{chain}," if chain else "[1:a]"
    voice_base_filter += f"volume={gain:.1f}dB"

    # Calcula os delays em ms
    delays = []
    if repeat_every > 0:
        # primeira ocorrência na position pedida
        first_delay = _delay_ms(position, music_ms, voice_ms)
        delays.append(first_delay)
        # repetições subsequentes
        max_repeats = repeat_count if repeat_count > 0 else int(music_s / repeat_every) + 2
        for i in range(1, max_repeats):
            d = first_delay + int(i * repeat_every * 1000)
            if d + voice_ms < music_ms:
                delays.append(d)
            else:
                break
    else:
        delays.append(_delay_ms(position, music_ms, voice_ms))

    # Monta o filter_complex com múltiplas vozes atrasadas
    voice_filters = []
    for idx, d in enumerate(delays):
        label = f"voice{idx}"
        voice_filters.append(f"{voice_base_filter},adelay={d}:all=1[{label}]")

    # Ducking em TODAS as ocorrências (a música abaixa e volta em cada uma).
    duck = ",".join(_duck_filters(delays, voice_s))

    voice_inputs = "".join(f"[{v}]" for v in [f"voice{i}" for i in range(len(delays))])
    amix_inputs = len(delays) + 1

    tmp = music_path + ".wmtmp" + ext
    try:
        cmd = [
            ffmpeg, "-y",
            "-i", music_path,
            "-i", voice,
            "-filter_complex",
            f"[0:a]{duck}[music];" + ";".join(voice_filters) + ";"
            f"[music]{voice_inputs}amix=inputs={amix_inputs}:duration=first:dropout_transition=0:normalize=0[aout]",
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
        log.info("assinatura aplicada em %s (posição=%s, repeat_every=%.1fs, count=%d, volume=%.2f)",
                 os.path.basename(music_path), position, repeat_every, len(delays), vol)
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
