package com.lagoagents.mobile.alerts

import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.lagoagents.mobile.R
import com.lagoagents.mobile.data.AppSettings
import com.lagoagents.mobile.data.Session
import com.lagoagents.mobile.net.LagoException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Live alerts: a connection your computer pushes through. The service keeps one request
 * to /v1/events open; the computer answers it the moment something needs you (within a
 * second), or every 50 seconds as a heartbeat, and the service asks again at once.
 *
 * When the network changes (Wi-Fi to mobile data, Tailscale reconnecting) the open
 * request is dropped and a new one made right away; when the computer can't be reached,
 * it retries every few seconds, at most [SHORT_CAP_MS] apart for the first ten minutes and
 * [MAX_BACKOFF_MS] after that, and at once when the network comes back or the app opens.
 * Android requires a visible notification for this; it's silent and shows the
 * connection's state, with a button to switch live alerts off.
 */
class LiveAlertsService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var loop: Job? = null
    @Volatile private var call: okhttp3.Call? = null
    private val networkBack = Channel<Unit>(Channel.CONFLATED)
    private var status: String? = null

    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        private var current: Network? = null

        override fun onAvailable(network: Network) {
            val previous = current
            current = network
            if (previous != null && previous != network) reconnect()  // not the first report
            else networkBack.trySend(Unit)                             // back from no network
        }

        override fun onLost(network: Network) {
            if (network == current) current = null
            reconnect()
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        running = this
        runCatching { getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(networkCallback) }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            AppSettings.get(this).update { it.copy(instantAlerts = false) }
            stopSelf()
            return START_NOT_STICKY
        }
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
            ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0
        ServiceCompat.startForeground(this, NOTIFICATION_ID, notification("Connecting to your computer…"), type)
        if (loop?.isActive != true) loop = scope.launch { listen() }
        else networkBack.trySend(Unit)  // the app was opened: if we're waiting to retry, retry now
        return START_STICKY
    }

    private suspend fun listen() {
        var since = 0L
        var backoff = FIRST_BACKOFF_MS
        var outageSince = 0L
        while (scope.isActive) {
            val client = Session.client(applicationContext)
            if (client == null) {  // signed out or unpaired
                stopSelf()
                return
            }
            try {
                val current = client.eventsCall(since).also { call = it }
                val answer = client.readEvents(current)
                showStatus("Connected · you'll hear the moment something needs you")
                backoff = FIRST_BACKOFF_MS
                outageSince = 0L
                com.lagoagents.mobile.update.Updater.announced(applicationContext, answer.optJSONObject("app"))
                com.lagoagents.mobile.voice.SpeechPlayer.announced(applicationContext, answer.optJSONArray("speech"), answer.optLong("hush"))
                com.lagoagents.mobile.call.Ringer.announced(applicationContext, answer.optJSONObject("call"))
                val version = answer.optLong("version")
                if (version != since) {
                    since = version
                    answer.optJSONArray("notifications")?.let { Alerts.process(applicationContext, it) }
                }
            } catch (signedOut: LagoException.SignedOut) {
                Session.dropToken(applicationContext)
                stopSelf()
                return
            } catch (error: Exception) {
                if (!scope.isActive) return
                if (call?.isCanceled() == true) {  // dropped on purpose for a network change
                    since = 0
                    continue
                }
                showStatus("Can't reach your computer · retrying (is Tailscale on?)")
                // Wait, but no longer than it takes the network to come back.
                withTimeoutOrNull(backoff) { networkBack.receive() }
                if (outageSince == 0L) outageSince = System.currentTimeMillis()
                // Retry quickly for a while (the computer was busy or the network blinked),
                // then gently once it's clearly off.
                val cap = if (System.currentTimeMillis() - outageSince < LONG_OUTAGE_MS) SHORT_CAP_MS else MAX_BACKOFF_MS
                backoff = (backoff * 2).coerceAtMost(cap)
                since = 0  // after a gap, take the whole list again
            }
        }
    }

    /** The network changed: drop the request that may hang on the old one and ask again now. */
    private fun reconnect() {
        call?.cancel()
        networkBack.trySend(Unit)
    }

    private fun showStatus(text: String) {
        if (text == status) return
        status = text
        runCatching { NotificationManagerCompat.from(this).notify(NOTIFICATION_ID, notification(text)) }
    }

    private fun notification(text: String) = NotificationCompat.Builder(this, Alerts.CHANNEL_LIVE)
        .setSmallIcon(R.drawable.ic_stat_lago)
        .setContentTitle("Live alerts")
        .setContentText(text)
        .setOngoing(true)
        .setSilent(true)
        .setPriority(NotificationCompat.PRIORITY_MIN)
        .setContentIntent(Alerts.openIntent(this, "#overview", 2))
        .addAction(0, "Switch off", PendingIntent.getService(this, 0,
            Intent(this, LiveAlertsService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
        .build()

    override fun onDestroy() {
        if (running === this) running = null
        runCatching { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(networkCallback) }
        call?.cancel()
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val ACTION_STOP = "stop"
        private const val NOTIFICATION_ID = 7443
        private const val FIRST_BACKOFF_MS = 2_000L
        private const val SHORT_CAP_MS = 15_000L
        private const val MAX_BACKOFF_MS = 60_000L
        private const val LONG_OUTAGE_MS = 10 * 60_000L

        fun start(context: Context) {
            runCatching { ContextCompat.startForegroundService(context, Intent(context, LiveAlertsService::class.java)) }
        }

        /** Reconnect now (if running), so the computer learns at once that the app opened or closed. */
        fun kick(@Suppress("UNUSED_PARAMETER") context: Context) {
            running?.reconnect()
        }

        @Volatile private var running: LiveAlertsService? = null

        fun stop(context: Context) {
            context.stopService(Intent(context, LiveAlertsService::class.java))
        }
    }
}
