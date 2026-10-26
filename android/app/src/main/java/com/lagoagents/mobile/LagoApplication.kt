package com.lagoagents.mobile

import android.app.Application
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import com.lagoagents.mobile.alerts.Alerts

class LagoApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        Alerts.createChannels(this)
        com.lagoagents.mobile.call.Ringer.createChannels(this)
        com.lagoagents.mobile.voice.Presence.voiceMode = com.lagoagents.mobile.data.AppSettings.get(this).value.voiceMode
        ProcessLifecycleOwner.get().lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStart(owner: LifecycleOwner) {
                inForeground = true
                com.lagoagents.mobile.voice.Presence.foreground = true
                com.lagoagents.mobile.alerts.LiveAlertsService.kick(this@LagoApplication)  // tell the computer now
            }
            override fun onStop(owner: LifecycleOwner) {
                inForeground = false
                com.lagoagents.mobile.voice.Presence.foreground = false
                com.lagoagents.mobile.alerts.LiveAlertsService.kick(this@LagoApplication)
                com.lagoagents.mobile.update.Updater.onBackground(this@LagoApplication)
            }
        })
    }

    companion object {
        /** While the app is on screen its own bell shows what needs you: no notifications. */
        @Volatile var inForeground = false
    }
}
