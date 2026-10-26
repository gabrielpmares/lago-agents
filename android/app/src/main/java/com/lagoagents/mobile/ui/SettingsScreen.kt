package com.lagoagents.mobile.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.lagoagents.mobile.BuildConfig
import com.lagoagents.mobile.data.Prefs
import com.lagoagents.mobile.data.Profile
import com.lagoagents.mobile.net.PinnedTrustManager
import com.lagoagents.mobile.update.UpdateState

private val VOICE_MODES = listOf(
    Triple("open", "On this phone while the app is open", "Lago talks here when you're using the app, and on the computer otherwise."),
    Triple("always", "On this phone, always", "Even with the app closed, through the live connection. Nothing plays on the computer."),
    Triple("off", "On the computer", "Lago talks at the desk, as before."),
)

private val LOCK_TIMES = listOf(0L to "Immediately", 60_000L to "After 1 minute", 300_000L to "After 5 minutes", 900_000L to "After 15 minutes")

/** The phone app's own settings: what the desktop pages can't know about. */
@Composable
fun SettingsScreen(
    profile: Profile?,
    prefs: Prefs,
    lockAvailable: Boolean,
    onPrefs: ((Prefs) -> Prefs) -> Unit,
    onAlertsChanged: () -> Unit,
    backgroundAllowed: Boolean,
    onAllowBackground: () -> Unit,
    update: UpdateState,
    onUpdate: () -> Unit,
    onCheckUpdate: () -> Unit,
    onSignOut: () -> Unit,
    onUnpair: () -> Unit,
    onClose: () -> Unit,
) {
    BackHandler(onBack = onClose)
    var confirmUnpair by remember { mutableStateOf(false) }
    var pickLockTime by remember { mutableStateOf(false) }

    Surface(color = MaterialTheme.colorScheme.background, modifier = Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing)) {
            Row(Modifier.fillMaxWidth().height(56.dp).padding(horizontal = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = onClose) { Icon(Icons.Filled.ArrowBack, contentDescription = "Back") }
                Text("App settings", style = MaterialTheme.typography.titleLarge)
            }
            Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(18.dp)) {

                Section("Your computer") {
                    Info("Address", profile?.serverUrl ?: "Not paired", mono = true)
                    Info("This phone", profile?.deviceName ?: "")
                    profile?.let { Info("Certificate (SHA-256)", PinnedTrustManager.pretty(it.fingerprint), mono = true) }
                }

                Section("Alerts") {
                    Toggle("Live alerts", "Your computer pushes alerts the moment an agent asks something, finishes or crashes. Keeps a quiet permanent notification; idle until there's news.",
                        prefs.instantAlerts) { on -> onPrefs { it.copy(instantAlerts = on) }; onAlertsChanged() }
                    if (prefs.instantAlerts && !backgroundAllowed) {
                        Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                            Text("Battery optimization can delay live alerts while the phone sleeps.",
                                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.tertiary,
                                modifier = Modifier.weight(1f).padding(end = 8.dp))
                            OutlinedButton(onClick = onAllowBackground, shape = RoundedCornerShape(10.dp)) { Text("Allow") }
                        }
                    }
                    Toggle("Backup check", "Every 15 minutes, in case the live connection was stopped by the phone.",
                        prefs.backgroundAlerts) { on -> onPrefs { it.copy(backgroundAlerts = on) }; onAlertsChanged() }
                }

                Section("Lago's voice") {
                    for ((mode, title, description) in VOICE_MODES) {
                        Row(
                            Modifier.fillMaxWidth().clickable { onPrefs { it.copy(voiceMode = mode) }; onAlertsChanged() }.padding(vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            RadioButton(selected = prefs.voiceMode == mode, onClick = null)
                            Column(Modifier.padding(start = 12.dp)) {
                                Text(title, style = MaterialTheme.typography.bodyLarge)
                                Text(description, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    }
                }

                Section("Calls") {
                    val context = LocalContext.current
                    Text("Call Lago from his page, or long-press the app icon. He rings you when something has waited too long; " +
                        "choose when in Lago's Settings on the computer.",
                        style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(vertical = 8.dp))
                    if (!prefs.instantAlerts) {
                        Text("He can only ring you while Live alerts are on.", style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.tertiary, modifier = Modifier.padding(vertical = 8.dp))
                    }
                    if (!com.lagoagents.mobile.call.Ringer.canRingFullScreen(context)) {
                        Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                            Text("Allow his calls to show full screen, over the lock screen, like a phone call.",
                                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.tertiary,
                                modifier = Modifier.weight(1f).padding(end = 8.dp))
                            OutlinedButton(onClick = {
                                runCatching { context.startActivity(android.content.Intent(android.provider.Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT,
                                    android.net.Uri.parse("package:${context.packageName}"))) }
                            }, shape = RoundedCornerShape(10.dp)) { Text("Allow") }
                        }
                    }
                }

                Section("Privacy") {
                    Toggle("App lock", if (lockAvailable) "Your fingerprint, face or screen lock to open the app."
                        else "This phone has no screen lock, so the app asks for the phone password instead.",
                        prefs.lockEnabled) { on -> onPrefs { it.copy(lockEnabled = on) } }
                    if (prefs.lockEnabled) {
                        Row(Modifier.fillMaxWidth().clickable { pickLockTime = true }.padding(vertical = 10.dp)) {
                            Column(Modifier.weight(1f)) {
                                Text("Lock again", style = MaterialTheme.typography.bodyLarge)
                                Text(LOCK_TIMES.firstOrNull { it.first == prefs.lockAfterMs }?.second ?: "After 1 minute",
                                    style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    }
                    Toggle("Hide from screenshots", "Blocks screenshots and blanks the app in the recent-apps list.",
                        prefs.secureScreen) { on -> onPrefs { it.copy(secureScreen = on) } }
                }

                Section("Terminals") {
                    Toggle("Fit to the phone's width", "While you look at a session, it's narrowed so Claude Code redraws for the phone. The desktop gets its width back when you close it.",
                        prefs.terminalFit) { on -> onPrefs { it.copy(terminalFit = on) } }
                }

                Section("App version") {
                    Info("Installed", "${BuildConfig.VERSION_NAME} (version ${BuildConfig.VERSION_CODE})")
                    when (update) {
                        is UpdateState.Available -> Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                            Text("Version ${update.release.name} is ready", style = MaterialTheme.typography.bodyLarge, modifier = Modifier.weight(1f))
                            OutlinedButton(onClick = onUpdate, shape = RoundedCornerShape(10.dp)) { Text("Update") }
                        }
                        is UpdateState.Downloading -> Text("Downloading ${update.release.name}… ${(update.progress * 100).toInt()}%",
                            style = MaterialTheme.typography.bodyLarge, modifier = Modifier.padding(vertical = 10.dp))
                        is UpdateState.Installing -> Text("Installing ${update.release.name}…",
                            style = MaterialTheme.typography.bodyLarge, modifier = Modifier.padding(vertical = 10.dp))
                        UpdateState.None -> TextButton(onClick = onCheckUpdate) { Text("Check for updates") }
                    }
                    Toggle("Update automatically", "New versions from your computer install while the app isn't on screen. Off: you're notified and tap Update.",
                        prefs.autoUpdate) { on -> onPrefs { it.copy(autoUpdate = on) } }
                }

                Section("Session") {
                    OutlinedButton(onClick = onSignOut, modifier = Modifier.fillMaxWidth().height(48.dp), shape = RoundedCornerShape(12.dp)) {
                        Text("Sign out (keeps the pairing)")
                    }
                    TextButton(onClick = { confirmUnpair = true }, modifier = Modifier.fillMaxWidth()) {
                        Text("Forget this computer", color = MaterialTheme.colorScheme.error)
                    }
                }
                Text("Lago Agents for Android ${BuildConfig.VERSION_NAME}. Your agents run on your computer; this app shows them and steers them.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.size(24.dp))
            }
        }
    }

    if (confirmUnpair) {
        AlertDialog(
            onDismissRequest = { confirmUnpair = false },
            title = { Text("Forget this computer?") },
            text = { Text("This phone signs out and deletes its key. To come back, pair again with a new code from the computer.") },
            confirmButton = { TextButton(onClick = { confirmUnpair = false; onUnpair() }) { Text("Forget", color = MaterialTheme.colorScheme.error) } },
            dismissButton = { TextButton(onClick = { confirmUnpair = false }) { Text("Cancel") } },
        )
    }
    if (pickLockTime) {
        AlertDialog(
            onDismissRequest = { pickLockTime = false },
            title = { Text("Lock again") },
            text = {
                Column {
                    for ((ms, label) in LOCK_TIMES) {
                        Text(label, style = MaterialTheme.typography.bodyLarge,
                            color = if (ms == prefs.lockAfterMs) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurface,
                            modifier = Modifier.fillMaxWidth().clickable { onPrefs { it.copy(lockAfterMs = ms) }; pickLockTime = false }.padding(vertical = 12.dp))
                    }
                }
            },
            confirmButton = { TextButton(onClick = { pickLockTime = false }) { Text("Close") } },
        )
    }
}

@Composable
private fun Section(title: String, content: @Composable () -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(title.uppercase(), style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Surface(shape = RoundedCornerShape(14.dp), color = MaterialTheme.colorScheme.surface, tonalElevation = 0.dp) {
            Column(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 6.dp)) { content() }
        }
    }
}

@Composable
private fun Info(label: String, value: String, mono: Boolean = false) {
    Column(Modifier.fillMaxWidth().padding(vertical = 8.dp)) {
        Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, style = MaterialTheme.typography.bodyMedium, fontFamily = if (mono) FontFamily.Monospace else null)
    }
    HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
}

@Composable
private fun Toggle(title: String, description: String, checked: Boolean, enabled: Boolean = true, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth().toggleable(value = checked, enabled = enabled, role = Role.Switch, onValueChange = onChange)
            .padding(vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f).padding(end = 12.dp)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(description, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Switch(checked = checked, onCheckedChange = null, enabled = enabled)
    }
}
