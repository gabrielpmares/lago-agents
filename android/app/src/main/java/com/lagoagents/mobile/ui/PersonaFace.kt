package com.lagoagents.mobile.ui

import androidx.annotation.DrawableRes
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.keyframes
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.ColorMatrix
import androidx.compose.ui.graphics.TransformOrigin
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.lagoagents.mobile.R

/** What the face is doing, as on the desktop (the orb's data-state in styles.css). */
enum class Mood { Idle, Listening, Thinking, Speaking, Ringing, Asleep }

/** A persona's face in layers (persona-faces.py makes them from the desktop's drawings). */
private class Face(
    @DrawableRes val face: Int,
    @DrawableRes val eyes: Int? = null, val eyesY: Float = 0.5f,
    @DrawableRes val smile: Int? = null,
    @DrawableRes val mouth: Int? = null, val mouthY: Float = 0.5f,
    @DrawableRes val bulb: Int? = null,
    @DrawableRes val glow: Int? = null,
)

private val FACES = mapOf(
    "lago" to Face(R.drawable.persona_lago_face, R.drawable.persona_lago_eyes, 22.6f / 48,
        R.drawable.persona_lago_smile, R.drawable.persona_lago_mouth, 32.4f / 48),
    "chip" to Face(R.drawable.persona_chip_face, R.drawable.persona_chip_eyes, 19.4f / 48,
        R.drawable.persona_chip_smile, R.drawable.persona_chip_mouth, 25.1f / 48, bulb = R.drawable.persona_chip_bulb),
    "atlas" to Face(R.drawable.persona_atlas_face, bulb = R.drawable.persona_atlas_bulb, glow = R.drawable.persona_atlas_glow),
    // Marina, Ada and Iris: the otter, Chip and Atlas in their own colours.
    "marina" to Face(R.drawable.persona_marina_face, R.drawable.persona_marina_eyes, 22.6f / 48,
        R.drawable.persona_marina_smile, R.drawable.persona_marina_mouth, 32.4f / 48),
    "ada" to Face(R.drawable.persona_ada_face, R.drawable.persona_ada_eyes, 19.4f / 48,
        R.drawable.persona_ada_smile, R.drawable.persona_ada_mouth, 25.1f / 48, bulb = R.drawable.persona_ada_bulb),
    "iris" to Face(R.drawable.persona_iris_face, bulb = R.drawable.persona_iris_bulb, glow = R.drawable.persona_iris_glow),
)

/** His face at rest as a picture, for notifications (a caller's photo). */
fun personaBitmap(context: android.content.Context, persona: String, px: Int = 192): android.graphics.Bitmap {
    val face = FACES[persona] ?: FACES.getValue("lago")
    val bitmap = android.graphics.Bitmap.createBitmap(px, px, android.graphics.Bitmap.Config.ARGB_8888)
    val canvas = android.graphics.Canvas(bitmap)
    val disc = android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG).apply { color = 0xFF16241F.toInt() }
    canvas.drawCircle(px / 2f, px / 2f, px / 2f, disc)  // round, like a caller's photo
    val inset = px / 9
    for (layer in listOfNotNull(face.face, face.eyes, face.smile, face.bulb)) {
        androidx.core.content.ContextCompat.getDrawable(context, layer)?.apply {
            setBounds(inset, inset, px - inset, px - inset)
            draw(canvas)
        }
    }
    return bitmap
}

/** The persona id for a name ("Chip" → "chip"); Lago for anyone else. */
fun personaId(name: String?): String = name?.lowercase()?.takeIf { it in FACES } ?: "lago"

private val GREY = ColorFilter.colorMatrix(ColorMatrix().apply { setToSaturation(0f) })

/**
 * Lago, Chip or Atlas, moving as he does on the desktop: he blinks, tilts his head to listen,
 * looks about (or blinks his bulb) while he thinks, talks, and hops when he's calling you.
 */
@Composable
fun PersonaFace(persona: String, mood: Mood, size: Dp, modifier: Modifier = Modifier) {
    val face = FACES[persona] ?: FACES.getValue("lago")
    val moving = rememberInfiniteTransition(label = "face")
    val blink by moving.animateFloat(1f, 1f, infiniteRepeatable(keyframes {
        durationMillis = 4600
        1f at 4230; 0.1f at 4370; 1f at 4500
    }), label = "blink")
    val talk by moving.animateFloat(0.35f, 0.35f, infiniteRepeatable(keyframes {
        // Uneven openings, like syllables, rather than an even pulse.
        durationMillis = 1000
        1f at 140; 0.5f at 280; 0.85f at 440; 0.2f at 580; 0.95f at 720; 0.45f at 860
    }), label = "talk")
    val look by moving.animateFloat(-1f, 1f, infiniteRepeatable(tween(1200, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "look")
    val pulse by moving.animateFloat(0.35f, 1f, infiniteRepeatable(tween(500, easing = LinearEasing), RepeatMode.Reverse), label = "pulse")
    val glowing by moving.animateFloat(0.25f, 1f, infiniteRepeatable(tween(300), RepeatMode.Reverse), label = "glow")
    val hop by moving.animateFloat(0f, 0f, infiniteRepeatable(keyframes {
        durationMillis = 1400
        -2.5f at 420; 0f at 840
    }), label = "hop")
    val tilt by animateFloatAsState(if (mood == Mood.Listening) -9f else 0f, tween(300), label = "tilt")
    val unit = size.value / 48f  // a viewBox unit, in dp
    val speaking = mood == Mood.Speaking
    val asleep = mood == Mood.Asleep
    val filter = if (asleep) GREY else null

    Box(modifier.size(size).graphicsLayer {
        rotationZ = tilt
        translationY = if (mood == Mood.Ringing) hop * unit * density else 0f
        transformOrigin = TransformOrigin(0.5f, 40f / 48)
        alpha = if (asleep) 0.7f else 1f
    }) {
        Layer(face.face, filter)
        face.eyes?.let { eyes ->
            if (!asleep) Layer(eyes, filter, Modifier.graphicsLayer {
                scaleY = blink
                translationX = if (mood == Mood.Thinking) look * 1.2f * unit * density else 0f
                translationY = if (mood == Mood.Thinking) -1.4f * unit * density else 0f
                transformOrigin = TransformOrigin(0.5f, face.eyesY)
            })
        }
        face.smile?.let { if (!speaking) Layer(it, filter) }
        face.mouth?.let { mouth ->
            if (speaking) Layer(mouth, filter, Modifier.graphicsLayer {
                scaleY = talk
                transformOrigin = TransformOrigin(0.5f, face.mouthY)
            })
        }
        face.bulb?.let { Layer(it, filter, Modifier.graphicsLayer { alpha = if (mood == Mood.Thinking) pulse else 1f }) }
        face.glow?.let { glow ->
            val shown = when (mood) { Mood.Speaking -> glowing; Mood.Thinking -> pulse * 0.5f; else -> 0f }
            if (shown > 0f) Layer(glow, filter, Modifier.graphicsLayer {
                alpha = shown
                val scale = if (speaking) 0.85f + 0.15f * glowing else 1f
                scaleX = scale; scaleY = scale
                transformOrigin = TransformOrigin(0.5f, 25f / 48)
            })
        }
    }
}

@Composable
private fun Layer(@DrawableRes id: Int, filter: ColorFilter?, modifier: Modifier = Modifier) {
    Image(painterResource(id), contentDescription = null, colorFilter = filter, modifier = modifier.fillMaxSize())
}

/** The three side by side, as on the welcome screen. */
@Composable
fun PersonaTrio(size: Dp = 64.dp, modifier: Modifier = Modifier) {
    Row(modifier, horizontalArrangement = Arrangement.spacedBy(size / 4)) {
        PersonaFace("chip", Mood.Idle, size * 0.86f, Modifier.graphicsLayer { translationY = size.toPx() * 0.12f })
        PersonaFace("lago", Mood.Idle, size)
        PersonaFace("atlas", Mood.Idle, size * 0.86f, Modifier.graphicsLayer { translationY = size.toPx() * 0.12f })
    }
}
