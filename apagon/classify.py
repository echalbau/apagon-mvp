"""Clasificación de mensajes (corte / bajón / restablecimiento) y geocodificación por gazetteer.

Es deliberadamente simple y auditable. Cuando cambies los patrones, sube PARSER_VERSION y corre
`python -m apagon reprocess` para reclasificar el histórico guardado.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

PARSER_VERSION = "regex-v1"


def normalize(text: str) -> str:
    """Minúsculas, sin acentos (ñ → n) y espacios colapsados."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip()


_LUZ = r"(?:(?:la|el)\s+)?(?:luz|electricidad|corriente|energia(?:\s+electrica)?|servicio\s+electrico)"

# El orden importa: un mensaje de restablecimiento suele mencionar también el corte
# ("volvió la luz tras 6 h sin luz"), y un corte pesa más que un bajón.
EVENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "restore",
        re.compile(
            rf"\b(?:llego|volvio|regreso|pusieron|restablecieron|restituyeron)\s+{_LUZ}\b"
            r"|\bya\s+(?:hay|tenemos)\s+luz\b"
        ),
    ),
    (
        "outage",
        re.compile(
            rf"(?<!no )\bse\s+(?:nos\s+)?fue\s+{_LUZ}\b"
            rf"|\b(?:nos\s+)?quitaron\s+{_LUZ}\b"
            r"|\bsin\s+(?:luz|electricidad|corriente)\b"
            r"|\bno\s+hay\s+luz\b"
            r"|\bapagon(?:es)?\b"
            r"|\bcorte\s+(?:electrico|de\s+luz|de\s+energia)\b"
            r"|\bracionamiento\b"
        ),
    ),
    (
        "dip",
        re.compile(
            r"\bbajon(?:es|azo|azos)?\b"
            r"|\bfluctuacion(?:es)?\b"
            r"|\b(?:baja(?:da)?|caida)s?\s+de\s+(?:voltaje|tension)\b"
            r"|\bsube\s+y\s+baja\s+la\s+luz\b"
            r"|\bparpadea\w*\s+la\s+luz\b"
        ),
    ),
)


def classify(text: str, normalized: bool = False) -> str | None:
    t = text if normalized else normalize(text)
    for label, pattern in EVENT_PATTERNS:
        if pattern.search(t):
            return label
    return None


class Geocoder:
    """Asigna un estado buscando alias (ciudades, municipios, sectores) en el texto.

    Las alternativas se ordenan de mayor a menor longitud para que "catia la mar" (La Guaira)
    gane sobre "catia" (Caracas) y "san carlos del zulia" sobre "san carlos" (Cojedes).
    """

    def __init__(self, aliases: dict[str, str]):
        self.aliases = {normalize(a): r for a, r in aliases.items() if a and a.strip()}
        alternatives = sorted(self.aliases, key=len, reverse=True)
        self._rx = (
            re.compile(r"\b(?:" + "|".join(re.escape(a) for a in alternatives) + r")\b")
            if alternatives
            else None
        )

    @classmethod
    def from_db(cls, conn) -> "Geocoder":
        rows = conn.execute("SELECT alias, region_id FROM region_aliases").fetchall()
        return cls({r[0]: r[1] for r in rows})

    def locate(self, text: str, default: str | None = None, normalized: bool = False) -> str | None:
        if self._rx is None:
            return default
        t = text if normalized else normalize(text)
        hits = [self.aliases[m.group(0)] for m in self._rx.finditer(t)]
        if not hits:
            return default
        # Más mencionado; en empate, el primero que aparece en el texto.
        return Counter(hits).most_common(1)[0][0]


def parse_message(text: str, geocoder: Geocoder, default_region: str | None = None) -> tuple[str | None, str | None]:
    t = normalize(text)
    return classify(t, normalized=True), geocoder.locate(t, default=default_region, normalized=True)
