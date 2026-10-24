package com.lagoagents.mobile.terminal

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material.icons.filled.Send
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.SmallFloatingActionButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.lagoagents.mobile.data.Prefs
import com.lagoagents.mobile.ui.TerminalState
import kotlinx.coroutines.flow.distinctUntilChanged

private val TermBg = Color(0xFF0B0C0E)
private val TermFg = Color(0xFFE8E9EB)
private val TermDim = Color(0xFF80858E)
private val TermBorder = Color(0xFF272A30)
private val TermKey = Color(0xFF1C1F23)
private val Accent = Color(0xFF4FB89E)
private val Warn = Color(0xFFD6A54A)

/** Keys worth a button: answering Claude Code's prompts, moving through its menus. */
private val KEYS = listOf(
    "Enter" to "Enter ⏎", "Escape" to "Esc", "Up" to "↑", "Down" to "↓", "Tab" to "Tab",
    "1" to "1", "2" to "2", "3" to "3", "y" to "y", "n" to "n", "BTab" to "⇧Tab", "C-c" to "Ctrl+C",
)

/**
 * A live mirror of a terminal on your computer: what the agent's tmux session shows,
 * refreshed every second, with buttons for the keys its prompts need and a line to type
 * into it. Nothing runs here; it's the same session the desktop app shows.
 */
@Composable
fun TerminalPanel(
    state: TerminalState,
    prefs: Prefs,
    title: String,
    onClose: () -> Unit,
    onToggleExpanded: () -> Unit,
    onKey: (String) -> Unit,
    onSend: (String) -> Unit,
    onColumns: (Int) -> Unit,
    onPrefs: ((Prefs) -> Prefs) -> Unit,
    onFit: (Boolean) -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier.background(TermBg)) {
        Header(state, prefs, title, onClose, onToggleExpanded, onPrefs, onFit)
        BoxWithConstraints(Modifier.weight(1f).fillMaxWidth()) {
            val measurer = rememberTextMeasurer()
            val density = LocalDensity.current
            val style = TextStyle(fontFamily = FontFamily.Monospace, fontSize = prefs.terminalFontSp.sp, lineHeight = (prefs.terminalFontSp * 1.3f).sp)
            val charWidth = remember(prefs.terminalFontSp) {
                measurer.measure(AnnotatedString("MMMMMMMMMM"), style).size.width / 10f
            }
            val columns = with(density) { ((maxWidth - 20.dp).toPx() / charWidth).toInt() }
            LaunchedEffect(columns, state.sessionId) { onColumns(columns) }
            TerminalText(state, prefs, style)
        }
        KeyRow(onKey)
        InputRow(onSend)
    }
}

@Composable
private fun Header(
    state: TerminalState, prefs: Prefs, title: String, onClose: () -> Unit, onToggleExpanded: () -> Unit,
    onPrefs: ((Prefs) -> Prefs) -> Unit, onFit: (Boolean) -> Unit,
) {
    Row(
        Modifier.fillMaxWidth().height(44.dp).border(width = 1.dp, color = TermBorder).padding(start = 12.dp, end = 2.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(8.dp).clip(CircleShape).background(if (state.error != null) Warn else Accent))
        Spacer(Modifier.width(8.dp))
        Text(title, color = TermFg, fontSize = 13.sp, fontWeight = FontWeight.Medium, maxLines = 1,
            overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        Toggle("Fit", prefs.terminalFit, "Narrow the session to the phone's width") { onFit(!prefs.terminalFit) }
        Toggle("Wrap", prefs.terminalWrap, "Wrap long lines") { onPrefs { it.copy(terminalWrap = !it.terminalWrap) } }
        TextButtonSmall("A−", "Smaller text") { onPrefs { it.copy(terminalFontSp = (it.terminalFontSp - 1f).coerceAtLeast(7f)) } }
        TextButtonSmall("A+", "Bigger text") { onPrefs { it.copy(terminalFontSp = (it.terminalFontSp + 1f).coerceAtMost(18f)) } }
        IconButton(onClick = onToggleExpanded) {
            Icon(if (state.expanded) Icons.Filled.KeyboardArrowDown else Icons.Filled.KeyboardArrowUp,
                contentDescription = if (state.expanded) "Show the page too" else "Full screen", tint = TermFg)
        }
        IconButton(onClick = onClose) { Icon(Icons.Filled.Close, contentDescription = "Hide the terminal", tint = TermFg) }
    }
}

@Composable
private fun Toggle(label: String, on: Boolean, description: String, onClick: () -> Unit) {
    Text(
        label, fontSize = 12.sp, fontWeight = FontWeight.Medium,
        color = if (on) Accent else TermDim,
        modifier = Modifier.clip(RoundedCornerShape(6.dp)).clickable(onClick = onClick)
            .semantics { contentDescription = "$description: ${if (on) "on" else "off"}" }
            .padding(horizontal = 7.dp, vertical = 8.dp),
    )
}

@Composable
private fun TextButtonSmall(label: String, description: String, onClick: () -> Unit) {
    Text(label, fontSize = 13.sp, color = TermFg,
        modifier = Modifier.clip(RoundedCornerShape(6.dp)).clickable(onClick = onClick)
            .semantics { contentDescription = description }.padding(horizontal = 7.dp, vertical = 8.dp))
}

@Composable
private fun TerminalText(state: TerminalState, prefs: Prefs, style: TextStyle) {
    val vertical = rememberScrollState()
    val horizontal = rememberScrollState()
    var follow by remember(state.sessionId) { mutableStateOf(true) }
    val text = remember(state.lines) { annotate(state.lines) }

    // Stay at the bottom as output arrives, unless you scrolled up to read.
    LaunchedEffect(vertical) {
        snapshotFlow { vertical.maxValue }.distinctUntilChanged().collect { if (follow) vertical.scrollTo(it) }
    }
    LaunchedEffect(vertical) {
        snapshotFlow { vertical.isScrollInProgress }.collect { scrolling ->
            if (!scrolling) follow = vertical.value >= vertical.maxValue - 24
        }
    }

    Box(Modifier.fillMaxSize()) {
        val scrollModifier = Modifier.fillMaxSize().verticalScroll(vertical)
            .let { if (prefs.terminalWrap) it else it.horizontalScroll(horizontal) }
            .padding(horizontal = 10.dp, vertical = 8.dp)
        SelectionContainer(scrollModifier) {
            when {
                !state.loaded -> Text("Connecting to the session…", color = TermDim, style = style)
                state.lines.isEmpty() -> Text("(The screen is empty.)", color = TermDim, style = style)
                else -> Text(text, color = TermFg, style = style, softWrap = prefs.terminalWrap)
            }
        }
        state.error?.let {
            Text(it, color = Warn, fontSize = 12.sp,
                modifier = Modifier.align(Alignment.TopCenter).padding(8.dp)
                    .background(TermKey, RoundedCornerShape(8.dp)).padding(horizontal = 10.dp, vertical = 6.dp))
        }
        if (!follow) {
            SmallFloatingActionButton(
                onClick = { follow = true },
                containerColor = TermKey, contentColor = TermFg,
                modifier = Modifier.align(Alignment.BottomEnd).padding(12.dp),
            ) { Icon(Icons.Filled.KeyboardArrowDown, contentDescription = "Jump to the latest output") }
        }
    }
    LaunchedEffect(follow) { if (follow) vertical.scrollTo(vertical.maxValue) }
}

private fun annotate(lines: List<TerminalLine>): AnnotatedString = buildAnnotatedString {
    lines.forEachIndexed { index, line ->
        for (span in line.spans) {
            val start = length
            append(span.text)
            val fg = span.fg?.let { Color(it) }
            addStyle(
                SpanStyle(
                    color = (fg ?: Color.Unspecified).let { if (span.dim) (fg ?: TermFg).copy(alpha = 0.6f) else it },
                    background = span.bg?.let { Color(it) } ?: Color.Unspecified,
                    fontWeight = if (span.bold) FontWeight.Bold else null,
                    fontStyle = if (span.italic) FontStyle.Italic else null,
                    textDecoration = if (span.underline) TextDecoration.Underline else null,
                ),
                start, length,
            )
        }
        if (index < lines.lastIndex) append('\n')
    }
}

@Composable
private fun KeyRow(onKey: (String) -> Unit) {
    Row(
        Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(horizontal = 8.dp, vertical = 6.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        for ((key, label) in KEYS) {
            Text(
                label, color = if (key == "Enter") Accent else TermFg, fontSize = 13.sp,
                fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Medium,
                modifier = Modifier.clip(RoundedCornerShape(8.dp)).background(TermKey)
                    .border(1.dp, TermBorder, RoundedCornerShape(8.dp))
                    .clickable { onKey(key) }
                    .semantics { contentDescription = "Press $key" }
                    .heightIn(min = 40.dp).padding(horizontal = 14.dp, vertical = 10.dp),
            )
        }
    }
}

@Composable
private fun InputRow(onSend: (String) -> Unit) {
    var text by rememberSaveable { mutableStateOf("") }
    val send = {
        if (text.isNotEmpty()) {
            onSend(text)
            text = ""
        }
    }
    Row(Modifier.fillMaxWidth().padding(start = 8.dp, end = 4.dp, bottom = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        TextField(
            value = text, onValueChange = { text = it },
            placeholder = { Text("Type into the session…", color = TermDim, fontSize = 14.sp) },
            textStyle = TextStyle(color = TermFg, fontSize = 15.sp, fontFamily = FontFamily.Monospace),
            maxLines = 4,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send, autoCorrectEnabled = false),
            keyboardActions = KeyboardActions(onSend = { send() }),
            colors = TextFieldDefaults.colors(
                focusedContainerColor = TermKey, unfocusedContainerColor = TermKey,
                focusedIndicatorColor = Color.Transparent, unfocusedIndicatorColor = Color.Transparent,
                cursorColor = Accent,
            ),
            shape = RoundedCornerShape(12.dp),
            modifier = Modifier.weight(1f),
        )
        IconButton(onClick = send, enabled = text.isNotEmpty()) {
            Icon(Icons.Filled.Send, contentDescription = "Send (types it and presses Enter)", tint = if (text.isNotEmpty()) Accent else TermDim)
        }
    }
}
