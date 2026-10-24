package com.lagoagents.mobile.ui

import android.webkit.WebView
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import com.lagoagents.mobile.data.Prefs
import com.lagoagents.mobile.terminal.TerminalPanel
import com.lagoagents.mobile.update.UpdateState

/**
 * The app proper: the Lago Agents pages from your computer, and under them, when a page
 * shows a terminal, the live mirror of that session.
 */
@Composable
fun HomeScreen(
    webView: WebView,
    terminal: TerminalState?,
    prefs: Prefs,
    offline: String?,
    error: String?,
    onRetry: () -> Unit,
    onErrorShown: () -> Unit,
    onBack: () -> Unit,
    update: UpdateState,
    onUpdate: () -> Unit,
    vm: AppViewModel,
) {
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(error) {
        if (error != null) {
            snackbar.showSnackbar(error)
            onErrorShown()
        }
    }
    BackHandler(onBack = onBack)

    Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background).windowInsetsPadding(WindowInsets.safeDrawing)) {
        BoxWithConstraints(Modifier.fillMaxSize()) {
            val panelHeight = maxHeight * 0.46f
            Column(Modifier.fillMaxSize()) {
                AnimatedVisibility(visible = offline != null) { OfflineBanner(offline.orEmpty(), onRetry) }
                UpdateBanner(update, onUpdate)
                if (terminal?.expanded != true) {
                    AndroidView(factory = { webView }, modifier = Modifier.weight(1f).fillMaxWidth())
                }
                if (terminal != null) {
                    TerminalPanel(
                        state = terminal,
                        prefs = prefs,
                        title = terminalTitle(terminal.sessionId),
                        onClose = { vm.closeTerminal() },
                        onToggleExpanded = { vm.toggleTerminalExpanded() },
                        onKey = vm::terminalKey,
                        onSend = vm::terminalSend,
                        onColumns = vm::setTerminalColumns,
                        onPrefs = { change -> vm.settings.update(change) },
                        onFit = vm::setTerminalFit,
                        modifier = if (terminal.expanded) Modifier.weight(1f).fillMaxWidth() else Modifier.height(panelHeight).fillMaxWidth(),
                    )
                }
            }
        }
        SnackbarHost(snackbar, Modifier.align(Alignment.BottomCenter).padding(bottom = 72.dp))
    }
}

private fun terminalTitle(id: String) = when {
    id == "orchestrator" -> "Lago's terminal"
    id.matches(Regex("^w\\d+$")) -> "Work session #${id.drop(1)}"
    id.startsWith("deploy-") -> "Deploy · ${id.removePrefix("deploy-")}"
    id.startsWith("ssh-") -> "SSH · ${id.removePrefix("ssh-")}"
    id.startsWith("login-") -> "Logging in · ${id.removePrefix("login-")}"
    else -> "Terminal · $id"
}

@Composable
private fun OfflineBanner(message: String, onRetry: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.tertiary.copy(alpha = 0.16f))
            .padding(start = 14.dp, end = 4.dp, top = 6.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(Icons.Filled.Warning, contentDescription = null, tint = MaterialTheme.colorScheme.tertiary, modifier = Modifier.size(20.dp))
        Spacer(Modifier.size(10.dp))
        Text(message, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f), maxLines = 3)
        TextButton(onClick = onRetry) { Text("Try Again") }
    }
}
