package com.lagoagents.mobile.call

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.Person
import androidx.core.content.ContextCompat
import androidx.core.graphics.drawable.IconCompat
import com.lagoagents.mobile.R
import com.lagoagents.mobile.alerts.Alerts
import com.lagoagents.mobile.data.Session
import com.lagoagents.mobile.ui.personaBitmap
import com.lagoagents.mobile.ui.personaId
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * Lago ringing you: the live connection carries the call he started (calls.py) and this
 * shows it as an incoming call, full screen over the lock screen, with the phone's ringtone.
 * When it stops being offered (it rang out, or what it was about was dealt with) the ringing
 * stops; if you never answered, a missed-call notification says who called and why.
 */
object Ringer {
    const val CHANNEL_RINGING = "calls"
    const val CHANNEL_ONGOING = "call-ongoing"
    private const val CHANNEL_MISSED = "calls-missed"
    private const val RING_ID = 7451
    private const val MISSED_ID = 7455  // the updater has 7444-7446
    private const val ACTION_DECLINE = "com.lagoagents.mobile.call.DECLINE"
    const val EXTRA_CALL = "call"
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** The call ringing now, as the computer last described it. */
    @Volatile var ringing: JSONObject? = null
        private set
    private var answered: String? = null

    fun createChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_RINGING, "Calls from Lago", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Lago ringing you when something needs you"
                setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE),
                    AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 800, 600, 800, 600)
                lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
            })
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ONGOING, "Ongoing call", NotificationManager.IMPORTANCE_LOW).apply {
                description = "While you're on a call with Lago"
                setShowBadge(false)
            })
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_MISSED, "Missed calls", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "When Lago called and you didn't answer"
            })
    }

    /** What the live connection says about a call (null: none). */
    @Synchronized
    fun announced(context: Context, call: JSONObject?) {
        val offered = call?.takeIf { it.optString("state") == "ringing" }
        val before = ringing
        if (offered != null && offered.optString("id") != before?.optString("id")) {
            ringing = offered
            if (!CallService.onCall) ring(context, offered)
        } else if (offered == null && before != null) {
            ringing = null
            NotificationManagerCompat.from(context).cancel(RING_ID)
            val id = before.optString("id")
            if (answered != id && call?.optString("id") != id) missed(context, before)
        }
    }

    /** You answered (the call screen does the rest). */
    fun answer(context: Context, id: String) {
        answered = id
        NotificationManagerCompat.from(context).cancel(RING_ID)
    }

    fun decline(context: Context, id: String) {
        answered = id  // not missed: you saw it
        NotificationManagerCompat.from(context).cancel(RING_ID)
        scope.launch { runCatching { Session.client(context)?.call("call_end", JSONObject().put("id", id).put("how", "declined")) } }
    }

    private fun canNotify(context: Context) =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    /** Whether Android lets the call show full screen over the lock screen (Android 14 asks you). */
    fun canRingFullScreen(context: Context) =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE ||
            context.getSystemService(NotificationManager::class.java).canUseFullScreenIntent()

    private fun ring(context: Context, call: JSONObject) {
        if (!canNotify(context)) return
        val id = call.optString("id")
        val name = call.optJSONObject("persona")?.optString("name") ?: "Lago"
        val about = call.optJSONObject("reason")?.optString("title").orEmpty()
        val screen = PendingIntent.getActivity(context, RING_ID,
            Intent(context, CallActivity::class.java).putExtra(EXTRA_CALL, call.toString())
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_USER_ACTION),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val answer = PendingIntent.getActivity(context, RING_ID + 1,
            Intent(context, CallActivity::class.java).putExtra(EXTRA_CALL, call.toString())
                .putExtra(CallActivity.EXTRA_ANSWER_NOW, true).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val decline = PendingIntent.getBroadcast(context, RING_ID + 2,
            Intent(context, DeclineReceiver::class.java).setAction(ACTION_DECLINE).putExtra(EXTRA_CALL, id),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val face = personaBitmap(context, call.optJSONObject("persona")?.optString("id") ?: personaId(name))
        val caller = Person.Builder().setName(name).setIcon(IconCompat.createWithBitmap(face)).setImportant(true).build()
        val notification = NotificationCompat.Builder(context, CHANNEL_RINGING)
            .setSmallIcon(R.drawable.ic_stat_lago)
            .setColor(0xFF4FB89E.toInt())
            .setContentTitle("$name is calling")
            .setContentText(about.ifBlank { "Something needs you" })
            .setStyle(NotificationCompat.CallStyle.forIncomingCall(caller, decline, answer))
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setFullScreenIntent(screen, true)
            .setContentIntent(screen)
            .setOngoing(true)
            .setTimeoutAfter(60_000)
            .build()
        notification.flags = notification.flags or android.app.Notification.FLAG_INSISTENT  // ring until answered
        runCatching { NotificationManagerCompat.from(context).notify(RING_ID, notification) }
    }

    private fun missed(context: Context, call: JSONObject) {
        if (!canNotify(context)) return
        val name = call.optJSONObject("persona")?.optString("name") ?: "Lago"
        val reason = call.optJSONObject("reason")
        val text = listOfNotNull(reason?.optString("title")?.takeIf { it.isNotBlank() },
            reason?.optString("context")?.takeIf { it.isNotBlank() }).joinToString(" · ").ifBlank { "He'll tell you when you call back." }
        val notification = NotificationCompat.Builder(context, CHANNEL_MISSED)
            .setSmallIcon(R.drawable.ic_stat_lago)
            .setColor(0xFF4FB89E.toInt())
            .setContentTitle("Missed call from $name")
            .setLargeIcon(personaBitmap(context, call.optJSONObject("persona")?.optString("id") ?: personaId(name)))
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setCategory(NotificationCompat.CATEGORY_MISSED_CALL)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setAutoCancel(true)
            .setContentIntent(Alerts.openIntent(context, reason?.optString("href")?.ifBlank { null } ?: "#orchestrator", MISSED_ID))
            .addAction(0, "Call back", PendingIntent.getActivity(context, MISSED_ID + 1,
                Intent(context, CallActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE))
            .build()
        runCatching { NotificationManagerCompat.from(context).notify(MISSED_ID, notification) }
    }

    class DeclineReceiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action != ACTION_DECLINE) return
            val id = intent.getStringExtra(EXTRA_CALL) ?: return
            decline(context.applicationContext, id)
        }
    }
}
