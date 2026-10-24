package com.lagoagents.mobile.alerts

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.lagoagents.mobile.LagoApplication
import com.lagoagents.mobile.MainActivity
import com.lagoagents.mobile.R
import com.lagoagents.mobile.data.AppSettings
import com.lagoagents.mobile.data.Session
import com.lagoagents.mobile.net.LagoException
import org.json.JSONArray
import java.util.concurrent.TimeUnit

/**
 * Phone notifications for what needs you: the same list as the bell in the app (a plan
 * to approve, an agent asking permission or a question, a result to check, a crash).
 * Each item notifies once; tapping it opens its page.
 */
object Alerts {
    const val CHANNEL_ATTENTION = "attention"
    const val CHANNEL_LIVE = "live"
    const val EXTRA_HREF = "href"
    private const val WORK = "lago-alerts"
    private const val PRIMED = "__primed__"
    private const val GROUP = "lago-needs-you"
    private const val SUMMARY_ID = 1

    fun createChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ATTENTION, context.getString(R.string.channel_attention), NotificationManager.IMPORTANCE_HIGH)
                .apply { description = context.getString(R.string.channel_attention_desc) }
        )
        manager.createNotificationChannel(
            NotificationChannel(com.lagoagents.mobile.update.Updater.CHANNEL, context.getString(R.string.channel_updates), NotificationManager.IMPORTANCE_DEFAULT)
                .apply { description = context.getString(R.string.channel_updates_desc) }
        )
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_LIVE, context.getString(R.string.channel_live), NotificationManager.IMPORTANCE_MIN)
                .apply { description = context.getString(R.string.channel_live_desc); setShowBadge(false) }
        )
    }

    /** Start or stop the periodic check and the live connection to match the settings. */
    fun reschedule(context: Context) {
        val prefs = AppSettings.get(context).value
        val signedIn = Session.profile(context)?.token != null
        val work = WorkManager.getInstance(context)
        if (signedIn && prefs.backgroundAlerts) {
            val request = PeriodicWorkRequestBuilder<AlertsWorker>(15, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            work.enqueueUniquePeriodicWork(WORK, ExistingPeriodicWorkPolicy.KEEP, request)
        } else {
            work.cancelUniqueWork(WORK)
        }
        // The live connection also carries Lago's voice to the phone.
        if (signedIn && (prefs.instantAlerts || prefs.voiceMode != "off")) LiveAlertsService.start(context)
        else LiveAlertsService.stop(context)
    }

    /** Ask the computer what needs you (the 15-minute backup check) and notify. */
    fun check(context: Context) {
        val client = Session.client(context) ?: return
        client.call("alerts").optJSONArray("notifications")?.let { process(context, it) }
        runCatching { com.lagoagents.mobile.update.Updater.check(context) }
    }

    /**
     * Notify about what's new in the list and withdraw notifications for what's no longer
     * in it (answered here or at the desk). The first list after pairing is only noted.
     * While the app is on screen its bell shows the list, so nothing new notifies then.
     */
    @Synchronized
    fun process(context: Context, items: JSONArray) {
        val settings = AppSettings.get(context)
        val seen = settings.seenAlerts().toMutableSet()
        val quiet = PRIMED !in seen || LagoApplication.inForeground
        val current = mutableSetOf<String>()
        val fresh = mutableListOf<Item>()
        for (i in 0 until items.length()) {
            val item = items.getJSONObject(i)
            val key = item.optString("key")
            if (key.isEmpty()) continue
            current += key
            if (key in seen) continue
            seen += key
            val title = item.optString("title", "Lago Agents")
            val body = item.optString("text").takeUnless { it.isBlank() || it.startsWith(title.removeSuffix("…")) }
            val text = listOfNotNull(item.optString("context").takeIf { it.isNotBlank() }, body).joinToString(" · ")
            fresh += Item(key, title, text, item.optString("href", "#overview"))
        }
        // Only what's still listed is worth remembering (keys change when the situation does).
        settings.setSeenAlerts(seen.intersect(current) + PRIMED)

        val manager = NotificationManagerCompat.from(context)
        val shown = settings.shownAlerts().toMutableSet()
        for (key in shown - current) manager.cancel(key.hashCode())
        shown.retainAll(current)
        if (!quiet) {
            for (item in fresh.take(6)) if (notify(context, item)) shown += item.key
        }
        settings.setShownAlerts(shown)
        if (shown.size > 1) summary(context, shown.size) else manager.cancel(SUMMARY_ID)
    }

    private data class Item(val key: String, val title: String, val text: String, val href: String)

    private fun canNotify(context: Context) =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    fun openIntent(context: Context, href: String, requestCode: Int): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra(EXTRA_HREF, href)
        return PendingIntent.getActivity(context, requestCode, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    private fun notify(context: Context, item: Item): Boolean {
        if (!canNotify(context)) return false
        val (key, title, text, href) = item
        val id = key.hashCode()
        val notification = NotificationCompat.Builder(context, CHANNEL_ATTENTION)
            .setSmallIcon(R.drawable.ic_stat_lago)
            .setColor(0xFF4FB89E.toInt())
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(
                NotificationCompat.Builder(context, CHANNEL_ATTENTION).setSmallIcon(R.drawable.ic_stat_lago)
                    .setContentTitle("Lago Agents").setContentText("Something needs you").build()
            )
            .setGroup(GROUP)
            .setAutoCancel(true)
            .setContentIntent(openIntent(context, href, id))
            .setWhen(System.currentTimeMillis())
            .setShowWhen(true)
            .build()
        return runCatching { NotificationManagerCompat.from(context).notify(id, notification) }.isSuccess
    }

    private fun summary(context: Context, count: Int) {
        if (!canNotify(context)) return
        val notification = NotificationCompat.Builder(context, CHANNEL_ATTENTION)
            .setSmallIcon(R.drawable.ic_stat_lago)
            .setColor(0xFF4FB89E.toInt())
            .setContentTitle("$count things need you")
            .setGroup(GROUP)
            .setGroupSummary(true)
            .setAutoCancel(true)
            .setContentIntent(openIntent(context, "#overview", SUMMARY_ID))
            .setSilent(true)
            .build()
        runCatching { NotificationManagerCompat.from(context).notify(SUMMARY_ID, notification) }
    }
}

class AlertsWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        try {
            Alerts.check(applicationContext)
        } catch (signedOut: LagoException.SignedOut) {
            Session.dropToken(applicationContext)
            WorkManager.getInstance(applicationContext).cancelUniqueWork("lago-alerts")
        } catch (_: Exception) {
            // Unreachable now (Tailscale off, computer asleep): try at the next interval.
        }
        return Result.success()
    }
}
