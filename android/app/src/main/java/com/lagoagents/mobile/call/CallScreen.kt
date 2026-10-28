package com.lagoagents.mobile.call

import androidx.annotation.DrawableRes
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.lagoagents.mobile.R
import com.lagoagents.mobile.ui.Mood
import com.lagoagents.mobile.ui.PersonaFace
import com.lagoagents.mobile.ui.personaId
import kotlinx.coroutines.delay
import org.json.JSONObject

/** Each persona's colours on the call: his own light over a dark screen, like a phone call. */
private class Tint(val top: Color, val accent: Color)

private val TINTS = mapOf(
    "lago" to Tint(Color(0xFF14453A), Color(0xFF4FB89E)),
    "chip" to Tint(Color(0xFF34301F), Color(0xFF7DFFB0)),
    "atlas" to Tint(Color(0xFF15304A), Color(0xFFECC97A)),
    "marina" to Tint(Color(0xFF4A1F2E), Color(0xFFE57F9F)),
    "ada" to Tint(Color(0xFF2E2140), Color(0xFFFF8FD8)),
    "iris" to Tint(Color(0xFF3A1840), Color(0xFFF4B8C8)),
)
private val BOTTOM = Color(0xFF0A0E11)
private val TEXT = Color(0xFFF3F6F5)
private val TEXT_2 = Color(0xB3F3F6F5)
private val TEXT_3 = Color(0x80F3F6F5)
private val DANGER = Color(0xFFE5484D)
private val ANSWER = Color(0xFF2FB36B)
private val WARN = Color(0xFFF2B84B)
private val PEOPLE = listOf("lago" to "Lago", "chip" to "Chip", "atlas" to "Atlas",
    "marina" to "Marina", "ada" to "Ada", "iris" to "Iris")

/**
 * What the call screen shows: Lago (or Chip, or Atlas) ringing you, the call itself with his
 * face moving as he listens, thinks and talks, or the call just ended.
 */
@Composable
fun CallScreen(
    ui: CallUi,
    ringing: JSONObject?,
    problem: String?,
    onAnswer: () -> Unit,
    onDecline: () -> Unit,
    onClose: () -> Unit,
    onMute: () -> Unit,
    onSpeaker: () -> Unit,
    onInterrupt: () -> Unit,
    onHangUp: () -> Unit,
    onTalkTo: (String) -> Unit,
) {
    val persona = ringing?.optJSONObject("persona")
    val name = persona?.optString("name")?.ifBlank { null } ?: ui.persona
    val id = persona?.optString("id")?.ifBlank { null } ?: ui.personaId.ifBlank { personaId(name) }
    val figure = persona?.optString("figure") ?: ui.figure
    val onCall = ringing == null && ui.phase != "ended"
    val tint = TINTS[id] ?: TINTS.getValue("lago")
    val top by animateColorAsState(tint.top, tween(600), label = "top")
    val accent by animateColorAsState(tint.accent, tween(600), label = "accent")
    val mood = when {
        ringing != null -> Mood.Ringing
        ui.phase == "ended" -> Mood.Asleep
        ui.phase == "speaking" -> Mood.Speaking
        ui.phase == "thinking" || ui.phase == "connecting" -> Mood.Thinking
        ui.muted -> Mood.Idle
        else -> Mood.Listening
    }

    Box(Modifier.fillMaxSize().background(Brush.verticalGradient(listOf(top, BOTTOM)))) {
        Column(Modifier.fillMaxSize().systemBarsPadding().padding(horizontal = 24.dp, vertical = 12.dp),
            horizontalAlignment = Alignment.CenterHorizontally) {
            Header(ringing != null, ui)
            Spacer(Modifier.weight(0.6f))
            Halo(mood, accent, 232.dp) { PersonaFace(id, mood, 168.dp) }
            Spacer(Modifier.height(12.dp))
            Text(if (ringing != null) "$name is calling" else name, color = TEXT, fontSize = 30.sp,
                fontWeight = FontWeight.SemiBold, letterSpacing = (-0.5).sp, textAlign = TextAlign.Center)
            if (figure.isNotBlank() && ringing == null) Text(figure, color = TEXT_3, fontSize = 14.sp)
            Spacer(Modifier.height(14.dp))
            if (ringing != null) Reason(ringing) else StatusPill(ui, name, accent)
            Spacer(Modifier.height(14.dp))
            Column(Modifier.widthIn(max = 420.dp).fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (onCall) ui.note?.let { Notice(it, WARN) }
                (problem ?: ui.error)?.let { Notice(it, DANGER) }
                if (onCall) Heard(ui.heard)
            }
            Spacer(Modifier.weight(1f))
            when {
                ringing != null -> Row(Modifier.fillMaxWidth().padding(bottom = 20.dp), horizontalArrangement = Arrangement.SpaceEvenly) {
                    BigButton("Decline", R.drawable.ic_call_end, DANGER, onClick = onDecline)
                    BigButton("Answer", null, ANSWER, pulse = true, onClick = onAnswer)
                }
                onCall -> {
                    TalkTo(id, enabled = ui.phase != "connecting", onTalkTo = onTalkTo)
                    Spacer(Modifier.height(22.dp))
                    Row(Modifier.widthIn(max = 380.dp).fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
                        Control(if (ui.muted) "Unmute" else "Mute", if (ui.muted) R.drawable.ic_call_mic_off else R.drawable.ic_call_mic,
                            on = ui.muted, onClick = onMute)
                        Control(if (ui.speaker) "Speaker" else "Earpiece", if (ui.speaker) R.drawable.ic_call_speaker else R.drawable.ic_call_earpiece,
                            on = ui.speaker, onClick = onSpeaker)
                        Control("Interrupt", R.drawable.ic_call_stop, on = false, enabled = ui.phase == "speaking", onClick = onInterrupt)
                    }
                    Spacer(Modifier.height(24.dp))
                    BigButton("Hang up", R.drawable.ic_call_end, DANGER, onClick = onHangUp)
                    Spacer(Modifier.height(12.dp))
                }
                else -> {
                    Box(Modifier.padding(bottom = 28.dp).clip(RoundedCornerShape(50)).background(Color.White.copy(alpha = 0.12f))
                        .clickable(onClick = onClose).padding(horizontal = 28.dp, vertical = 12.dp)) {
                        Text("Close", color = TEXT, fontWeight = FontWeight.Medium)
                    }
                }
            }
        }
    }
}

/** "Lago Agents" and, on the call, how long it's been going. */
@Composable
private fun Header(ringing: Boolean, ui: CallUi) {
    var now by remember { mutableLongStateOf(android.os.SystemClock.elapsedRealtime()) }
    val since = ui.since
    LaunchedEffect(since, ui.phase) {
        while (since != null && ui.phase != "ended") { now = android.os.SystemClock.elapsedRealtime(); delay(1000) }
    }
    val time = since?.let {
        val seconds = (now - it).coerceAtLeast(0) / 1000
        "%d:%02d".format(seconds / 60, seconds % 60)
    }
    Row(Modifier.padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Icon(painterResource(R.drawable.ic_stat_lago), contentDescription = null, tint = TEXT_3, modifier = Modifier.size(16.dp))
        Spacer(Modifier.width(6.dp))
        val label = when {
            ringing -> "Lago Agents call"
            ui.phase == "ended" -> listOfNotNull("Call ended", time).joinToString(" · ")
            else -> listOfNotNull("Lago Agents", time).joinToString(" · ")
        }
        Text(label, color = TEXT_3, fontSize = 13.sp, letterSpacing = 0.3.sp)
    }
}

/** Rings around the face: ripples while ringing or listening, a glow while he talks, a turning arc while he thinks. */
@Composable
private fun Halo(mood: Mood, accent: Color, size: Dp, face: @Composable () -> Unit) {
    val moving = rememberInfiniteTransition(label = "halo")
    val ripple by moving.animateFloat(0f, 1f, infiniteRepeatable(tween(1800, easing = LinearEasing)), label = "ripple")
    val breathe by moving.animateFloat(0f, 1f, infiniteRepeatable(tween(420), RepeatMode.Reverse), label = "breathe")
    val turn by moving.animateFloat(0f, 360f, infiniteRepeatable(tween(1400, easing = LinearEasing)), label = "turn")
    val color = if (mood == Mood.Ringing) WARN else accent
    Box(Modifier.size(size), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            val inner = this.size.minDimension * 0.36f
            val outer = this.size.minDimension / 2
            drawCircle(color.copy(alpha = if (mood == Mood.Asleep) 0.05f else 0.12f), radius = inner + 6.dp.toPx())
            when (mood) {
                Mood.Ringing, Mood.Listening -> for (k in 0 until 3) {
                    val t = (ripple + k / 3f) % 1f
                    drawCircle(color.copy(alpha = (1 - t) * 0.45f), radius = inner + (outer - inner) * t,
                        style = Stroke(2.dp.toPx()))
                }
                Mood.Speaking -> {
                    drawCircle(color.copy(alpha = 0.10f + 0.14f * breathe), radius = inner + 14.dp.toPx() + 10.dp.toPx() * breathe)
                    drawCircle(color.copy(alpha = 0.5f), radius = inner + 6.dp.toPx(), style = Stroke(2.dp.toPx()))
                }
                Mood.Thinking -> drawArc(color.copy(alpha = 0.8f), turn, 80f, useCenter = false,
                    topLeft = androidx.compose.ui.geometry.Offset(center.x - inner - 10.dp.toPx(), center.y - inner - 10.dp.toPx()),
                    size = androidx.compose.ui.geometry.Size((inner + 10.dp.toPx()) * 2, (inner + 10.dp.toPx()) * 2),
                    style = Stroke(3.dp.toPx(), cap = StrokeCap.Round))
                else -> {}
            }
        }
        face()
    }
}

@Composable
private fun StatusPill(ui: CallUi, name: String, accent: Color) {
    val (text, dot) = when (ui.phase) {
        "connecting" -> "Connecting…" to TEXT_3
        "listening" -> (if (ui.muted) "You're muted" else "Listening to you") to (if (ui.muted) TEXT_3 else DANGER)
        "thinking" -> "$name is thinking…" to WARN
        "speaking" -> "$name is talking" to accent
        else -> "Call ended" to TEXT_3
    }
    val blink = rememberInfiniteTransition(label = "dot")
    val alpha by blink.animateFloat(1f, 0.3f, infiniteRepeatable(tween(700), RepeatMode.Reverse), label = "alpha")
    Row(Modifier.clip(RoundedCornerShape(50)).background(Color.White.copy(alpha = 0.08f)).padding(horizontal = 14.dp, vertical = 7.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(8.dp).clip(CircleShape).background(dot.copy(alpha = if (ui.phase == "ended" || ui.muted) 1f else alpha)))
        Spacer(Modifier.width(8.dp))
        Text(text, color = TEXT, fontSize = 14.sp, fontWeight = FontWeight.Medium)
    }
}

/** Why he's calling: what it's about and where. */
@Composable
private fun Reason(call: JSONObject) {
    val reason = call.optJSONObject("reason")
    val title = reason?.optString("title")?.ifBlank { null } ?: "Something needs you"
    val context = reason?.optString("context")?.ifBlank { null }
    Column(Modifier.widthIn(max = 420.dp).fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(Color.White.copy(alpha = 0.08f))
        .padding(horizontal = 16.dp, vertical = 12.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Text(title, color = TEXT, fontSize = 16.sp, fontWeight = FontWeight.Medium, textAlign = TextAlign.Center)
        context?.let { Text(it, color = TEXT_2, fontSize = 13.sp, textAlign = TextAlign.Center) }
    }
}

@Composable
private fun Notice(text: String, color: Color) {
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(color.copy(alpha = 0.14f))
        .border(1.dp, color.copy(alpha = 0.35f), RoundedCornerShape(14.dp)).padding(horizontal = 14.dp, vertical = 10.dp)) {
        Text(text, color = TEXT, fontSize = 14.sp)
    }
}

/** What it heard you say, as you say it. */
@Composable
private fun Heard(text: String) {
    AnimatedVisibility(text.isNotBlank(), enter = fadeIn(), exit = fadeOut()) {
        Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(Color.White.copy(alpha = 0.06f))
            .padding(horizontal = 14.dp, vertical = 10.dp)) {
            Text("YOU", color = TEXT_3, fontSize = 11.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 1.sp)
            Spacer(Modifier.height(2.dp))
            Text(text, color = TEXT_2, fontSize = 15.sp, fontStyle = FontStyle.Italic, maxLines = 3, overflow = TextOverflow.Ellipsis)
        }
    }
}

/** Every persona, in a row that scrolls: tap one to hand them the call ("let me talk to Ada" works too). */
@Composable
private fun TalkTo(current: String, enabled: Boolean, onTalkTo: (String) -> Unit) {
    Row(Modifier.clip(RoundedCornerShape(50)).background(Color.White.copy(alpha = 0.06f))
        .horizontalScroll(rememberScrollState()).padding(5.dp),
        horizontalArrangement = Arrangement.spacedBy(4.dp), verticalAlignment = Alignment.CenterVertically) {
        for ((pid, label) in PEOPLE) {
            val on = pid == current
            val tint = TINTS.getValue(pid).accent
            Row(Modifier.clip(RoundedCornerShape(50))
                .background(if (on) tint.copy(alpha = 0.22f) else Color.Transparent)
                .clickable(enabled = enabled && !on, role = Role.Button) { onTalkTo(label) }
                .padding(start = 5.dp, end = 12.dp, top = 4.dp, bottom = 4.dp),
                verticalAlignment = Alignment.CenterVertically) {
                PersonaFace(pid, Mood.Idle, 30.dp)
                Spacer(Modifier.width(6.dp))
                Text(label, color = if (on) TEXT else TEXT_2, fontSize = 14.sp, fontWeight = if (on) FontWeight.SemiBold else FontWeight.Normal)
            }
        }
    }
}

@Composable
private fun Control(label: String, @DrawableRes icon: Int, on: Boolean, enabled: Boolean = true, onClick: () -> Unit) {
    val fade = if (enabled) 1f else 0.35f
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Box(Modifier.size(64.dp).clip(CircleShape)
            .background(if (on) Color.White.copy(alpha = 0.92f * fade) else Color.White.copy(alpha = 0.12f * fade))
            .clickable(enabled = enabled, role = Role.Button, onClickLabel = label, onClick = onClick),
            contentAlignment = Alignment.Center) {
            Icon(painterResource(icon), contentDescription = label, modifier = Modifier.size(28.dp),
                tint = (if (on) BOTTOM else TEXT).copy(alpha = fade))
        }
        Spacer(Modifier.height(8.dp))
        Text(label, color = TEXT_2.copy(alpha = 0.7f * fade + 0.3f), fontSize = 13.sp)
    }
}

/** Answer, decline or hang up. `icon` null is the phone handset (Answer). */
@Composable
private fun BigButton(label: String, @DrawableRes icon: Int?, color: Color, pulse: Boolean = false, onClick: () -> Unit) {
    val moving = rememberInfiniteTransition(label = "big")
    val grow by moving.animateFloat(1f, 1.08f, infiniteRepeatable(tween(700), RepeatMode.Reverse), label = "grow")
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Box(Modifier.size(76.dp).scale(if (pulse) grow else 1f).clip(CircleShape).background(color)
            .clickable(role = Role.Button, onClickLabel = label, onClick = onClick), contentAlignment = Alignment.Center) {
            if (icon != null) Icon(painterResource(icon), contentDescription = label, tint = Color.White, modifier = Modifier.size(34.dp))
            else Icon(Icons.Filled.Call, contentDescription = label, tint = Color.White, modifier = Modifier.size(32.dp))
        }
        Spacer(Modifier.height(8.dp))
        Text(label, color = TEXT_2, fontSize = 13.sp)
    }
}
