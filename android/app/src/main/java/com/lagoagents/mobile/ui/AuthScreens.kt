package com.lagoagents.mobile.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.lagoagents.mobile.R
import com.lagoagents.mobile.data.Profile
import com.lagoagents.mobile.net.PairingLink
import com.lagoagents.mobile.net.PinnedTrustManager

@Composable
private fun AuthFrame(content: @Composable ColumnScope.() -> Unit) {
    Surface(color = MaterialTheme.colorScheme.background, modifier = Modifier.fillMaxSize()) {
        Box(
            Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing).imePadding()
                .verticalScroll(rememberScrollState()),
            contentAlignment = Alignment.TopCenter,
        ) {
            Column(
                Modifier.widthIn(max = 460.dp).fillMaxWidth().padding(horizontal = 22.dp, vertical = 28.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp),
            ) { content() }
        }
    }
}

@Composable
fun Brand(subtitle: String) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(56.dp).clip(RoundedCornerShape(15.dp)).background(Color(0xFF16181B))) {
            Image(painterResource(R.drawable.ic_launcher_foreground), contentDescription = null, modifier = Modifier.fillMaxSize())
        }
        Spacer(Modifier.size(14.dp))
        Column {
            Text("Lago Agents", style = MaterialTheme.typography.headlineSmall)
            Text(subtitle, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun ErrorNote(error: String?) {
    if (error == null) return
    Row(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.error.copy(alpha = 0.12f)).padding(12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(Icons.Filled.Warning, contentDescription = null, tint = MaterialTheme.colorScheme.error)
        Spacer(Modifier.size(10.dp))
        Text(error, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurface)
    }
}

@Composable
private fun PasswordField(value: String, onChange: (String) -> Unit, label: String, onDone: () -> Unit) {
    var visible by rememberSaveable { mutableStateOf(false) }
    OutlinedTextField(
        value = value, onValueChange = onChange, label = { Text(label) }, singleLine = true,
        visualTransformation = if (visible) VisualTransformation.None else PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done, autoCorrectEnabled = false),
        keyboardActions = KeyboardActions(onDone = { onDone() }),
        trailingIcon = { TextButton(onClick = { visible = !visible }) { Text(if (visible) "Hide" else "Show") } },
        modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
private fun PrimaryButton(label: String, busy: Boolean, enabled: Boolean, onClick: () -> Unit) {
    Button(onClick = onClick, enabled = enabled && !busy, modifier = Modifier.fillMaxWidth().height(52.dp), shape = RoundedCornerShape(12.dp)) {
        if (busy) CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary)
        else Text(label)
    }
}

@Composable
private fun Hint(text: String) {
    Text(text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

/** First run: pair with your computer by its QR code (or typing its address and code). */
@Composable
fun PairScreen(busy: Boolean, error: String?, onScan: () -> Unit, scanned: PairingLink?, onPair: (PairingLink, String) -> Unit) {
    var manual by rememberSaveable { mutableStateOf(false) }
    var address by rememberSaveable { mutableStateOf("") }
    var code by rememberSaveable { mutableStateOf("") }
    var password by rememberSaveable { mutableStateOf("") }
    var localError by rememberSaveable { mutableStateOf<String?>(null) }
    val link = scanned?.takeIf { !manual }

    fun submit() {
        localError = null
        val target = link ?: run {
            val url = PairingLink.normalizeUrl(address)
            when {
                url == null -> { localError = "Enter your computer's address, like 100.101.102.103 or its Tailscale name"; return }
                PairingLink.normalizeCode(code).length != 10 -> { localError = "The pairing code has 10 letters and digits"; return }
                else -> PairingLink(url, null, code)
            }
        }
        if (password.isEmpty()) { localError = "Enter the phone password you set on your computer"; return }
        onPair(target, password)
    }

    AuthFrame {
        Brand("Your agents, from anywhere")
        Spacer(Modifier.height(4.dp))
        Text("Pair with your computer", style = MaterialTheme.typography.titleLarge)
        Hint("1. Turn Tailscale on, here and on your computer (same account).\n" +
            "2. On the computer, open Lago Agents, then Phone. Switch it on and press Pair a Phone.\n" +
            "3. Scan the code it shows, then enter the phone password.")

        if (link != null) {
            Surface(shape = RoundedCornerShape(12.dp), color = MaterialTheme.colorScheme.surfaceVariant) {
                Column(Modifier.fillMaxWidth().padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("Computer found", style = MaterialTheme.typography.titleMedium)
                    Text(link.url, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodyMedium)
                    link.fingerprint?.let {
                        Text("Certificate ${PinnedTrustManager.pretty(it).take(23)}…", fontFamily = FontFamily.Monospace,
                            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        } else if (!manual) {
            PrimaryButton("Scan the QR code", busy = false, enabled = true, onClick = onScan)
        }

        if (manual) {
            OutlinedTextField(
                value = address, onValueChange = { address = it }, singleLine = true,
                label = { Text("Computer's address") }, placeholder = { Text("100.x.y.z or name, port 7443") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Next, autoCorrectEnabled = false),
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = code, onValueChange = { code = it.uppercase().take(11) }, singleLine = true,
                label = { Text("Pairing code") }, placeholder = { Text("ABCDE-FGHJK") },
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Characters, imeAction = ImeAction.Next, autoCorrectEnabled = false),
                textStyle = MaterialTheme.typography.bodyLarge.copy(fontFamily = FontFamily.Monospace),
                modifier = Modifier.fillMaxWidth(),
            )
        }

        if (link != null || manual) {
            PasswordField(password, { password = it }, "Phone password", ::submit)
            ErrorNote(localError ?: error)
            PrimaryButton("Pair this phone", busy = busy, enabled = true, onClick = ::submit)
        } else {
            ErrorNote(error)
        }
        TextButton(onClick = { manual = !manual; localError = null }, modifier = Modifier.align(Alignment.CenterHorizontally)) {
            Text(if (manual) "Scan the QR code instead" else "Type the address and code instead")
        }
        Hint("The connection stays inside your Tailscale network and is pinned to your computer's own certificate. " +
            "The pairing code works once, for ten minutes.")
    }
}

/** The session lapsed (or you signed out): the phone password again. */
@Composable
fun LoginScreen(profile: Profile, busy: Boolean, error: String?, onLogin: (String) -> Unit, onForget: () -> Unit) {
    var password by rememberSaveable { mutableStateOf("") }
    AuthFrame {
        Brand("Sign in to your computer")
        Spacer(Modifier.height(4.dp))
        Surface(shape = RoundedCornerShape(12.dp), color = MaterialTheme.colorScheme.surfaceVariant) {
            Column(Modifier.fillMaxWidth().padding(14.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(profile.deviceName, style = MaterialTheme.typography.titleMedium)
                Text(profile.serverUrl, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        PasswordField(password, { password = it }, "Phone password") { if (password.isNotEmpty()) onLogin(password) }
        ErrorNote(error)
        PrimaryButton("Sign in", busy = busy, enabled = password.isNotEmpty()) { onLogin(password) }
        TextButton(onClick = onForget, modifier = Modifier.align(Alignment.CenterHorizontally)) {
            Text("Pair with a different computer")
        }
    }
}

/** The app lock: your fingerprint, face or screen lock. */
@Composable
fun LockScreen(available: Boolean, error: String?, onUnlock: () -> Unit, onUsePassword: () -> Unit) {
    LaunchedEffect(Unit) { if (available) onUnlock() }
    AuthFrame {
        Spacer(Modifier.height(48.dp))
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Box(Modifier.size(72.dp).clip(RoundedCornerShape(20.dp)).background(MaterialTheme.colorScheme.primaryContainer),
                contentAlignment = Alignment.Center) {
                Icon(Icons.Filled.Lock, contentDescription = null, tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(34.dp))
            }
            Text("Lago Agents is locked", style = MaterialTheme.typography.titleLarge)
            Text(
                if (available) "Unlock with your fingerprint, face or screen lock."
                else "This phone has no screen lock, so the app can't check it's you. Sign in with the phone password instead, or set a screen lock in Android's settings.",
                style = MaterialTheme.typography.bodyMedium, textAlign = TextAlign.Center,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            ErrorNote(error)
            if (available) PrimaryButton("Unlock", busy = false, enabled = true, onClick = onUnlock)
            OutlinedButton(onClick = onUsePassword, modifier = Modifier.fillMaxWidth().height(48.dp), shape = RoundedCornerShape(12.dp)) {
                Text("Sign in with the password")
            }
        }
    }
}
