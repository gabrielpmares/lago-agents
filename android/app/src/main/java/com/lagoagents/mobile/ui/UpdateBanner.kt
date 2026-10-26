package com.lagoagents.mobile.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.lagoagents.mobile.update.AppRelease
import com.lagoagents.mobile.update.UpdateState

/** A new version from your computer: what's new, and Update. Hidden when there's none. */
@Composable
fun UpdateBanner(state: UpdateState, onUpdate: () -> Unit) {
    var notesFor by remember { mutableStateOf<AppRelease?>(null) }
    val release = when (state) {
        is UpdateState.Available -> state.release
        is UpdateState.Downloading -> state.release
        is UpdateState.Installing -> state.release
        UpdateState.None -> return
    }
    Column(Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.primaryContainer)) {
        Row(Modifier.fillMaxWidth().padding(start = 14.dp, end = 4.dp, top = 4.dp, bottom = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(
                    when (state) {
                        is UpdateState.Downloading -> "Downloading version ${release.name}…"
                        is UpdateState.Installing -> if (state.confirm != null) "Version ${release.name}: Android asks for your OK"
                            else "Installing version ${release.name}…"
                        else -> "Version ${release.name} is ready"
                    },
                    style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurface,
                )
                (state as? UpdateState.Available)?.message?.let {
                    Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.tertiary)
                }
            }
            when (state) {
                is UpdateState.Available -> {
                    if (release.notes.isNotBlank()) TextButton(onClick = { notesFor = release }) { Text("What's new") }
                    TextButton(onClick = onUpdate) { Text("Update") }
                }
                is UpdateState.Installing -> if (state.confirm != null) {
                    TextButton(onClick = onUpdate) { Text("Finish") }
                } else {
                    CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.size(14.dp))
                }
                else -> {}
            }
        }
        if (state is UpdateState.Downloading) {
            LinearProgressIndicator(progress = { state.progress }, modifier = Modifier.fillMaxWidth())
        }
    }
    notesFor?.let { shown ->
        AlertDialog(
            onDismissRequest = { notesFor = null },
            title = { Text("What's new in ${shown.name}") },
            text = { Text(shown.notes, modifier = Modifier.verticalScroll(rememberScrollState())) },
            confirmButton = { TextButton(onClick = { notesFor = null; onUpdate() }) { Text("Update now") } },
            dismissButton = { TextButton(onClick = { notesFor = null }) { Text("Later") } },
        )
    }
}
