"""Chave pública de verificação das releases do AudioFlow.

A chave privada correspondente nunca é distribuída: fica guardada fora do
repo (em ``secrets/``) e também como secret ``AUDIOFLOW_SIGN_KEY`` no GitHub
Actions. A troca de chave exige nova release + divulgação; por isso, trate-a
como item de segurança do projeto.
"""

PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAQMZ0SOVFmvVjYn87/kKdvWmYmSDGUJ/J7so96MXrIpI=
-----END PUBLIC KEY-----"""
