r"""
tui/widgets/latex.py — LaTeX maths, transliterated into Unicode.

A model that is asked anything numerical answers in LaTeX, and a terminal
has no maths typesetting. What it does have is a large slice of Unicode:
greek letters, superscripts, subscripts, operators and arrows all exist as
real characters, so `\frac{\pi}{2}` can be shown as `π/2` instead of as its
own source code.

This is deliberately a *transliterator*, not a typesetter. Everything it
cannot map it leaves readable rather than dropping it: an unknown `\foo`
keeps its name, and a superscript with no Unicode form falls back to
`^(...)`. The worst case is that you read something close to the LaTeX you
were sent — never a blank where a formula was.

It also has to survive half a formula, because the chat renders while the
reply is still streaming: an unclosed brace takes the rest of the string
rather than raising.
"""

from __future__ import annotations

import re

_CMD = re.compile(r"\\([A-Za-z]+)\*?")

# ── Unicode scripts ───────────────────────────────────────────────────────────
# Only the characters that genuinely exist. A group containing anything else
# falls back to `^(...)`, which is still readable; a half-converted `x²ᵇq`
# would not be.

_SUPER = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
    "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼",
    "(": "⁽", ")": "⁾", "/": "ᐟ",
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ",
    "h": "ʰ", "i": "ⁱ", "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ",
    "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ", "v": "ᵛ",
    "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
    "A": "ᴬ", "B": "ᴮ", "D": "ᴰ", "E": "ᴱ", "G": "ᴳ", "H": "ᴴ", "I": "ᴵ",
    "J": "ᴶ", "K": "ᴷ", "L": "ᴸ", "M": "ᴹ", "N": "ᴺ", "O": "ᴼ", "P": "ᴾ",
    "R": "ᴿ", "T": "ᵀ", "U": "ᵁ", "V": "ⱽ", "W": "ᵂ",
}
# A script that is already a script maps to itself, so `e^{-x^2}` can finish
# as `e⁻ˣ²` instead of giving up at the `²` and falling back to `e^(-x²)`.
_SUPER.update({value: value for value in list(_SUPER.values())})

_SUB = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
    "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "−": "₋", "=": "₌",
    "(": "₍", ")": "₎",
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ",
    "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ",
    "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
}
_SUB.update({value: value for value in list(_SUB.values())})

# ── Symbols ───────────────────────────────────────────────────────────────────

_SYMBOLS = {
    # greek, lower
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ",
    "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω",
    # greek, upper
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω",
    # arithmetic
    "times": "×", "div": "÷", "pm": "±", "mp": "∓", "cdot": "·", "ast": "∗",
    "star": "⋆", "circ": "∘", "bullet": "∙", "oplus": "⊕", "ominus": "⊖",
    "otimes": "⊗", "oslash": "⊘", "odot": "⊙", "sqrt": "√",
    # relations
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "sim": "∼", "simeq": "≃", "cong": "≅",
    "propto": "∝", "ll": "≪", "gg": "≫", "doteq": "≐", "asymp": "≍",
    "prec": "≺", "succ": "≻", "preceq": "⪯", "succeq": "⪰",
    # sets and logic
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "setminus": "\\", "emptyset": "∅", "varnothing": "∅", "forall": "∀",
    "exists": "∃", "nexists": "∄", "neg": "¬", "lnot": "¬", "land": "∧",
    "lor": "∨", "wedge": "∧", "vee": "∨", "top": "⊤", "bot": "⊥",
    "models": "⊨", "vdash": "⊢",
    # arrows
    "to": "→", "rightarrow": "→", "longrightarrow": "⟶", "leftarrow": "←",
    "gets": "←", "longleftarrow": "⟵", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "implies": "⇒", "impliedby": "⇐", "iff": "⇔", "mapsto": "↦",
    "uparrow": "↑", "downarrow": "↓", "updownarrow": "↕", "hookrightarrow": "↪",
    # big operators
    "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬",
    "iiint": "∭", "oint": "∮", "bigcup": "⋃", "bigcap": "⋂",
    "bigoplus": "⨁", "bigotimes": "⨂", "bigwedge": "⋀", "bigvee": "⋁",
    # calculus and misc
    "partial": "∂", "nabla": "∇", "infty": "∞", "aleph": "ℵ", "hbar": "ℏ",
    "ell": "ℓ", "Re": "ℜ", "Im": "ℑ", "wp": "℘", "prime": "′",
    "degree": "°", "angle": "∠", "measuredangle": "∡", "perp": "⊥",
    "parallel": "∥", "therefore": "∴", "because": "∵", "surd": "√",
    "checkmark": "✓", "dagger": "†", "ddagger": "‡", "S": "§", "P": "¶",
    "copyright": "©", "pounds": "£", "euro": "€",
    # dots
    "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    # named operators — these stay words, they are read as words
    "sin": "sin", "cos": "cos", "tan": "tan", "cot": "cot", "sec": "sec",
    "csc": "csc", "arcsin": "arcsin", "arccos": "arccos", "arctan": "arctan",
    "sinh": "sinh", "cosh": "cosh", "tanh": "tanh", "log": "log", "ln": "ln",
    "lg": "lg", "exp": "exp", "min": "min", "max": "max", "sup": "sup",
    "inf": "inf", "lim": "lim", "limsup": "limsup", "liminf": "liminf",
    "det": "det", "dim": "dim", "deg": "deg", "gcd": "gcd", "arg": "arg",
    "ker": "ker", "mod": "mod", "bmod": "mod", "pmod": "mod",
}

# `\mathbb{R}` and friends — the double-struck letters people actually use.
_BLACKBOARD = {
    "A": "𝔸", "B": "𝔹", "C": "ℂ", "D": "𝔻", "E": "𝔼", "F": "𝔽", "G": "𝔾",
    "H": "ℍ", "I": "𝕀", "J": "𝕁", "K": "𝕂", "L": "𝕃", "M": "𝕄", "N": "ℕ",
    "O": "𝕆", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "S": "𝕊", "T": "𝕋", "U": "𝕌",
    "V": "𝕍", "W": "𝕎", "X": "𝕏", "Y": "𝕐", "Z": "ℤ",
}

# Combining marks. They render on top of the character before them, which is
# exactly what an accent is, and they measure as zero cells — so a line
# carrying one still lines up in a grid.
_ACCENTS = {
    "hat": "\u0302", "widehat": "\u0302", "bar": "\u0304", "overline": "\u0304",
    "vec": "\u20d7", "tilde": "\u0303", "widetilde": "\u0303",
    "dot": "\u0307", "ddot": "\u0308", "acute": "\u0301", "grave": "\u0300",
    "check": "\u030c", "breve": "\u0306", "mathring": "\u030a",
    "underline": "\u0332",
}

# Wrappers that only change a font: keep what is inside, drop the wrapper.
_PLAIN_WRAPPERS = {
    "mathrm", "mathbf", "mathit", "mathsf", "mathtt", "mathcal", "mathfrak",
    "mathscr", "boldsymbol", "bm", "operatorname", "textbf", "textit",
    "texttt", "textrm", "textsf", "emph", "displaystyle", "textstyle",
    "scriptstyle", "limits", "nolimits", "mathopen", "mathclose", "mathbin",
    "mathrel", "phantom", "hspace", "vspace",
}

# The vulgar fractions worth having — `½` reads better than `1/2`.
_VULGAR = {
    ("1", "2"): "½", ("1", "3"): "⅓", ("2", "3"): "⅔", ("1", "4"): "¼",
    ("3", "4"): "¾", ("1", "5"): "⅕", ("2", "5"): "⅖", ("3", "5"): "⅗",
    ("4", "5"): "⅘", ("1", "6"): "⅙", ("5", "6"): "⅚", ("1", "8"): "⅛",
    ("3", "8"): "⅜", ("5", "8"): "⅝", ("7", "8"): "⅞",
}

# Spacing macros. `\!` is negative space — the nearest honest rendering of
# "pull these together" is nothing at all.
_SPACING = {",": " ", ";": " ", ":": " ", " ": " ", "!": "", "quad": "  ",
            "qquad": "    ", "thinspace": " ", "enspace": " "}

_DELIMITERS = {"lbrace": "{", "rbrace": "}", "lvert": "|", "rvert": "|",
               "lVert": "‖", "rVert": "‖", "langle": "⟨", "rangle": "⟩",
               "lceil": "⌈", "rceil": "⌉", "lfloor": "⌊", "rfloor": "⌋",
               "vert": "|", "Vert": "‖", "backslash": "\\", "|": "‖"}


def render_math(source: str) -> str:
    """
    A LaTeX fragment as a plain string. Never raises — a formula that defeats
    it comes back as its own source, which is still information.
    """
    try:
        return _tidy(_transform(source))
    except Exception:
        return (source or "").strip()


# ── The walk ──────────────────────────────────────────────────────────────────

def _group(source: str, index: int) -> tuple[str, int]:
    """
    The argument starting at `index`, and where it ends.

    `{...}` is the usual form, but LaTeX also allows a bare token — `x^2`,
    `x^\\alpha` — and both appear constantly in generated maths. An unclosed
    brace takes the rest of the string: half a formula is normal while a
    reply is still arriving.
    """
    while index < len(source) and source[index] == " ":
        index += 1
    if index >= len(source):
        return "", index
    if source[index] != "{":
        command = _CMD.match(source, index)
        if command is not None:
            return command.group(0), command.end()
        return source[index], index + 1

    depth = 0
    out: list[str] = []
    i = index
    while i < len(source):
        char = source[i]
        if char == "\\" and i + 1 < len(source):
            out.append(source[i:i + 2])
            i += 2
            continue
        if char == "{":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif char == "}":
            depth -= 1
            if depth == 0:
                return "".join(out), i + 1
        out.append(char)
        i += 1
    return "".join(out), len(source)


def _script(body: str, marker: str) -> str:
    table = _SUPER if marker == "^" else _SUB
    if not body:
        return ""
    if all(char in table for char in body):
        return "".join(table[char] for char in body)
    return f"{marker}{body}" if len(body) == 1 else f"{marker}({body})"


def _needs_parens(part: str) -> bool:
    """
    A fraction only needs brackets when the slash would bind wrongly. `√` is
    in the list because `√π/2` genuinely reads two ways, and only one of them
    is what was written.
    """
    return bool(re.search(r"[+\-−*/^_ =√]", part.strip()))


def _fraction(top: str, bottom: str) -> str:
    vulgar = _VULGAR.get((top.strip(), bottom.strip()))
    if vulgar:
        return vulgar
    top = f"({top})" if _needs_parens(top) else top
    bottom = f"({bottom})" if _needs_parens(bottom) else bottom
    return f"{top}/{bottom}"


def _command(name: str, source: str, index: int) -> tuple[str, int] | None:
    """
    Commands that consume arguments. Returns (text, resume index), or None
    for anything that is just a symbol.
    """
    if name in ("frac", "dfrac", "tfrac", "cfrac"):
        top, index = _group(source, index)
        bottom, index = _group(source, index)
        return _fraction(_transform(top), _transform(bottom)), index

    if name == "sqrt":
        root = ""
        if index < len(source) and source[index] == "[":
            close = source.find("]", index)
            if close != -1:
                root = _script(_transform(source[index + 1:close]), "^")
                index = close + 1
        body, index = _group(source, index)
        body = _transform(body)
        return f"{root}√({body})" if _needs_parens(body) else f"{root}√{body}", index

    if name == "text":
        body, index = _group(source, index)
        return body, index           # prose, not maths — left exactly as written

    if name in _PLAIN_WRAPPERS:
        body, index = _group(source, index)
        return _transform(body), index

    if name == "mathbb":
        body, index = _group(source, index)
        return "".join(_BLACKBOARD.get(char, char) for char in body), index

    if name in _ACCENTS:
        body, index = _group(source, index)
        body = _transform(body)
        return "".join(char + _ACCENTS[name] for char in body) or body, index

    if name in ("begin", "end"):
        _env, index = _group(source, index)
        return "\n", index           # rows already break on `\\`

    if name in ("left", "right"):
        if index < len(source) and source[index] == "\\":
            command = _CMD.match(source, index)
            if command is not None:
                token = _DELIMITERS.get(command.group(1), "")
                return token, command.end()
            return source[index + 1], index + 2
        if index < len(source):
            # `\left.` is an invisible delimiter — it pairs a brace with nothing.
            return "" if source[index] == "." else source[index], index + 1
        return "", index

    if name in ("stackrel", "overset", "underset"):
        over, index = _group(source, index)
        under, index = _group(source, index)
        return f"{_transform(under)}{_transform(over)}", index

    return None


def _transform(source: str) -> str:
    out: list[str] = []
    i = 0
    length = len(source)

    while i < length:
        char = source[i]

        if char == "\\":
            command = _CMD.match(source, i)
            if command is not None:
                name = command.group(1)
                handled = _command(name, source, command.end())
                if handled is not None:
                    text, i = handled
                    out.append(text)
                    continue
                if name in _SPACING:
                    out.append(_SPACING[name])
                    i = command.end()
                    continue
                if name in _SYMBOLS:
                    out.append(_SYMBOLS[name])
                    i = command.end()
                    continue
                if name in _DELIMITERS:
                    out.append(_DELIMITERS[name])
                    i = command.end()
                    continue
                # Unknown, but with an argument: `\underbrace{x}` is still an
                # x. Keep the argument and drop the decoration.
                if command.end() < length and source[command.end()] == "{":
                    body, i = _group(source, command.end())
                    out.append(_transform(body))
                    continue
                out.append(name)
                i = command.end()
                continue

            following = source[i + 1] if i + 1 < length else ""
            if following == "\\":
                out.append("\n")            # row break
            elif following in _SPACING:
                out.append(_SPACING[following])
            else:
                out.append(following)       # \$ \% \& \_ \{ \} …
            i += 2
            continue

        if char in "^_":
            body, i = _group(source, i + 1)
            out.append(_script(_transform(body), char))
            continue

        if char == "{":
            body, i = _group(source, i)
            out.append(_transform(body))
            continue

        if char == "}":
            i += 1
            continue

        if char == "&":                     # alignment tab
            out.append("  ")
            i += 1
            continue

        if char == "~":                     # non-breaking space
            out.append(" ")
            i += 1
            continue

        out.append(char)
        i += 1

    return "".join(out)


def _tidy(text: str) -> str:
    lines = [re.sub(r"[ \t]{2,}", "  ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()
