package com.lagoagents.mobile.terminal

/** A run of text in one style. Colours are ARGB, or null for the terminal's default. */
data class Span(
    val text: String,
    val fg: Int? = null,
    val bg: Int? = null,
    val bold: Boolean = false,
    val dim: Boolean = false,
    val italic: Boolean = false,
    val underline: Boolean = false,
)

data class TerminalLine(val spans: List<Span>) {
    val text: String get() = spans.joinToString("") { it.text }
}

/**
 * Reads what `tmux capture-pane -e` prints: text with SGR escape sequences (colours,
 * bold, …). Other control sequences (cursor moves, OSC titles and links) are dropped.
 */
object Ansi {
    private data class Style(
        var fg: Int? = null, var bg: Int? = null, var bold: Boolean = false, var dim: Boolean = false,
        var italic: Boolean = false, var underline: Boolean = false, var inverse: Boolean = false,
    )

    fun parse(text: String): List<TerminalLine> {
        val lines = text.split('\n').map { parseLine(it.trimEnd('\r')) }.toMutableList()
        while (lines.isNotEmpty() && lines.last().text.isBlank()) lines.removeAt(lines.lastIndex)
        return lines
    }

    private fun parseLine(line: String): TerminalLine {
        val spans = ArrayList<Span>()
        val style = Style()
        val buffer = StringBuilder()
        fun flush() {
            if (buffer.isEmpty()) return
            val fg = if (style.inverse) style.bg ?: INVERSE_FG else style.fg
            val bg = if (style.inverse) style.fg ?: INVERSE_BG else style.bg
            spans += Span(buffer.toString(), fg, bg, style.bold, style.dim, style.italic, style.underline)
            buffer.setLength(0)
        }
        var i = 0
        while (i < line.length) {
            val c = line[i]
            if (c != '\u001b') {
                if (c == '\t') buffer.append("    ") else if (c >= ' ' || c == ' ') buffer.append(c)
                i++
                continue
            }
            val next = line.getOrNull(i + 1)
            when (next) {
                '[' -> {
                    var j = i + 2
                    while (j < line.length && line[j] !in '@'..'~') j++
                    if (j < line.length && line[j] == 'm') {
                        flush()
                        apply(style, line.substring(i + 2, j))
                    }
                    i = j + 1
                }
                ']' -> {  // OSC: up to BEL or ESC \
                    var j = i + 2
                    while (j < line.length && line[j] != '\u0007' && !(line[j] == '\u001b' && line.getOrNull(j + 1) == '\\')) j++
                    i = if (j < line.length && line[j] == '\u001b') j + 2 else j + 1
                }
                else -> i += 2
            }
        }
        flush()
        return TerminalLine(spans)
    }

    private fun apply(style: Style, params: String) {
        val codes = if (params.isEmpty()) listOf(0) else params.split(';', ':').map { it.toIntOrNull() ?: 0 }
        var k = 0
        while (k < codes.size) {
            when (val code = codes[k]) {
                0 -> { style.fg = null; style.bg = null; style.bold = false; style.dim = false
                    style.italic = false; style.underline = false; style.inverse = false }
                1 -> style.bold = true
                2 -> style.dim = true
                3 -> style.italic = true
                4 -> style.underline = true
                7 -> style.inverse = true
                22 -> { style.bold = false; style.dim = false }
                23 -> style.italic = false
                24 -> style.underline = false
                27 -> style.inverse = false
                in 30..37 -> style.fg = PALETTE[code - 30]
                39 -> style.fg = null
                in 40..47 -> style.bg = PALETTE[code - 40]
                49 -> style.bg = null
                in 90..97 -> style.fg = PALETTE[code - 90 + 8]
                in 100..107 -> style.bg = PALETTE[code - 100 + 8]
                38, 48 -> {
                    val mode = codes.getOrNull(k + 1)
                    val color: Int? = when (mode) {
                        5 -> codes.getOrNull(k + 2)?.let { xterm256(it) }.also { k += 2 }
                        2 -> {
                            val r = codes.getOrNull(k + 2) ?: 0
                            val g = codes.getOrNull(k + 3) ?: 0
                            val b = codes.getOrNull(k + 4) ?: 0
                            k += 4
                            rgb(r, g, b)
                        }
                        else -> null
                    }
                    if (code == 38) style.fg = color else style.bg = color
                }
            }
            k++
        }
    }

    private fun rgb(r: Int, g: Int, b: Int) =
        (0xFF shl 24) or ((r and 0xFF) shl 16) or ((g and 0xFF) shl 8) or (b and 0xFF)

    private fun xterm256(n: Int): Int = when {
        n < 16 -> PALETTE[n.coerceAtLeast(0)]
        n < 232 -> {
            val i = n - 16
            val steps = intArrayOf(0, 95, 135, 175, 215, 255)
            rgb(steps[i / 36], steps[(i / 6) % 6], steps[i % 6])
        }
        else -> (8 + (n.coerceAtMost(255) - 232) * 10).let { rgb(it, it, it) }
    }

    // A palette close to the desktop terminal's (VTE's defaults, softened for the dark theme).
    private val PALETTE = intArrayOf(
        0xFF2E3436.toInt(), 0xFFE26D69.toInt(), 0xFF4FB89E.toInt(), 0xFFD6A54A.toInt(),
        0xFF6C9BEF.toInt(), 0xFFB19CFF.toInt(), 0xFF4FB3C8.toInt(), 0xFFD3D7CF.toInt(),
        0xFF6B7078.toInt(), 0xFFF08A86.toInt(), 0xFF7ED9BF.toInt(), 0xFFF0C674.toInt(),
        0xFF8FB4FF.toInt(), 0xFFCBB9FF.toInt(), 0xFF7FD3E3.toInt(), 0xFFEEEEEC.toInt(),
    )
    private val INVERSE_FG = 0xFF0B0C0E.toInt()
    private val INVERSE_BG = 0xFFE8E9EB.toInt()
}
