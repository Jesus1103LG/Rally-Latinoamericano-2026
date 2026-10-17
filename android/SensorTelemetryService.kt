package com.agrometeo.station.service

import android.app.*
import android.content.Context
import android.content.Intent
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Base64
import android.util.Log
import androidx.core.app.NotificationCompat
import org.eclipse.paho.client.mqttv3.MqttClient
import org.eclipse.paho.client.mqttv3.MqttConnectOptions
import org.eclipse.paho.client.mqttv3.MqttMessage
import org.eclipse.paho.client.mqttv3.persist.MemoryPersistence
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * SensorTelemetryService
 *
 * Android Foreground Service designed for 24/7 unattended execution on recycled smartphones.
 * Features:
 *  1. Partial WakeLock management (keeps CPU alive while allowing display/GPU sleep).
 *  2. Hardware sensor acquisition (Barometer, Light, Ambient Temp/Humidity if present).
 *  3. PCM Audio streaming from the primary microphone (16 kHz, 16-bit Mono).
 *  4. High-throughput, zero-disk MQTT publication to Termux loopback (tcp://127.0.0.1:1883).
 */
class SensorTelemetryService : Service(), SensorEventListener {

    companion object {
        private const val TAG = "SensorTelemetryService"
        private const val NOTIFICATION_ID = 1001
        private const val CHANNEL_ID = "agrometeo_sensor_channel"

        // MQTT Loopback endpoint
        private const val BROKER_URL = "tcp://127.0.0.1:1883"
        private const val CLIENT_ID = "android_native_station"
        private const val TOPIC_SENSORS = "station/sensor/raw"
        private const val TOPIC_AUDIO = "station/audio/chunk"

        // Audio acquisition parameters
        // 16 kHz sample rate gives an 8 kHz Nyquist bandwidth: perfect for low-frequency
        // wind turbulence (< 500 Hz) while capturing voice and fauna harmonics for filtering.
        private const val SAMPLE_RATE = 16000
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT
    }

    private var wakeLock: PowerManager.WakeLock? = null
    private lateinit var sensorManager: SensorManager
    private var barometer: Sensor? = null
    private var lightSensor: Sensor? = null
    private var tempSensor: Sensor? = null
    private var humiditySensor: Sensor? = null

    private var mqttClient: MqttClient? = null
    private val executor = Executors.newSingleThreadScheduledExecutor()
    private val audioExecutor = Executors.newSingleThreadExecutor()

    // Sensor state cache
    private var latestPressureHpa: Float? = null
    private var latestLightLux: Float? = null
    private var latestTempC: Float? = null
    private var latestHumidityPct: Float? = null

    // Audio sampling state
    private val isRecording = AtomicBoolean(false)
    private var audioRecord: AudioRecord? = null

    override fun onCreate() {
        super.onCreate()
        acquireWakeLock()
        startForegroundServiceNotification()
        setupSensors()
        initializeMqttClient()
        scheduleDutyCycle()
    }

    private fun acquireWakeLock() {
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "AgroMeteo::SensorsWakeLock"
        ).apply {
            setReferenceCounted(false)
            acquire(24 * 60 * 60 * 1000L) // 24 hours renewal
        }
        Log.i(TAG, "Partial WakeLock acquired.")
    }

    private fun startForegroundServiceNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Agrometeorological Sensor Service",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Acquires hardware sensors and stream to Termux broker."
            }
            val manager = getSystemService(NotificationManager::class.java)
            manager?.createNotificationChannel(channel)
        }

        val notification: Notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("AgroMeteo Node Running")
            .setContentText("Reading barometer and acoustic wind sensor.")
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .build()

        startForeground(NOTIFICATION_ID, notification)
    }

    private fun setupSensors() {
        sensorManager = getSystemService(Context.SENSOR_SERVICE) as SensorManager
        barometer = sensorManager.getDefaultSensor(Sensor.TYPE_PRESSURE)
        lightSensor = sensorManager.getDefaultSensor(Sensor.TYPE_LIGHT)
        tempSensor = sensorManager.getDefaultSensor(Sensor.TYPE_AMBIENT_TEMPERATURE)
        humiditySensor = sensorManager.getDefaultSensor(Sensor.TYPE_RELATIVE_HUMIDITY)

        // Register hardware listeners with SENSOR_DELAY_NORMAL (low CPU overhead)
        barometer?.let { sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_NORMAL) }
        lightSensor?.let { sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_NORMAL) }
        tempSensor?.let { sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_NORMAL) }
        humiditySensor?.let { sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_NORMAL) }
    }

    private fun initializeMqttClient() {
        executor.execute {
            try {
                // MemoryPersistence avoids writing MQTT internal state to phone flash memory
                mqttClient = MqttClient(BROKER_URL, CLIENT_ID, MemoryPersistence())
                val connOpts = MqttConnectOptions().apply {
                    isCleanSession = true
                    connectionTimeout = 5
                    keepAliveInterval = 30
                    isAutomaticReconnect = true
                }
                mqttClient?.connect(connOpts)
                Log.i(TAG, "Connected to local Mosquitto broker at $BROKER_URL")
            } catch (e: Exception) {
                Log.e(TAG, "Error connecting to local Mosquitto: ${e.message}")
            }
        }
    }

    override fun onSensorChanged(event: SensorEvent?) {
        event ?: return
        when (event.sensor.type) {
            Sensor.TYPE_PRESSURE -> latestPressureHpa = event.values[0]
            Sensor.TYPE_LIGHT -> latestLightLux = event.values[0]
            Sensor.TYPE_AMBIENT_TEMPERATURE -> latestTempC = event.values[0]
            Sensor.TYPE_RELATIVE_HUMIDITY -> latestHumidityPct = event.values[0]
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    /**
     * Smart Duty-Cycling:
     * To conserve power and prevent battery overheating, we acquire acoustic audio
     * for a 10-second window every 5 minutes (or 10 minutes).
     */
    private fun scheduleDutyCycle() {
        // Runs periodic 10-second sampling bursts
        executor.scheduleWithFixedDelay({
            try {
                publishSensorTelemetry()
                recordAudioBurst(durationSeconds = 10)
            } catch (e: Exception) {
                Log.e(TAG, "Duty cycle execution error: ${e.message}", e)
            }
        }, 5, 300, TimeUnit.SECONDS) // 5 sec initial delay, 300 sec (5 min) cycle
    }

    private fun publishSensorTelemetry() {
        val payload = JSONObject().apply {
            put("timestamp_ms", System.currentTimeMillis())
            put("pressure_hpa", latestPressureHpa ?: JSONObject.NULL)
            put("light_lux", latestLightLux ?: JSONObject.NULL)
            put("ambient_temp_c", latestTempC ?: JSONObject.NULL)
            put("humidity_pct", latestHumidityPct ?: JSONObject.NULL)
        }

        publishMqtt(TOPIC_SENSORS, payload.toString().toByteArray())
    }

    private fun recordAudioBurst(durationSeconds: Int) {
        if (isRecording.getAndSet(true)) return

        audioExecutor.execute {
            val minBufSize = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT)
            val bufferSize = maxOf(minBufSize, 4096)
            val audioBuffer = ShortArray(bufferSize / 2)

            try {
                // Use UNPROCESSED or MIC audio source to bypass Android's built-in noise suppression
                // which actively removes wind noise (the very signal we want to measure!).
                val audioSource = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                    MediaRecorder.AudioSource.UNPROCESSED
                } else {
                    MediaRecorder.AudioSource.MIC
                }

                audioRecord = AudioRecord(audioSource, SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT, bufferSize)
                if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
                    Log.e(TAG, "AudioRecord initialization failed.")
                    return@execute
                }

                audioRecord?.startRecording()
                Log.i(TAG, "Audio burst acquisition started ($durationSeconds seconds).")

                val endTime = System.currentTimeMillis() + (durationSeconds * 1000L)
                val byteBuffer = ByteArray(audioBuffer.size * 2)

                while (System.currentTimeMillis() < endTime && isRecording.get()) {
                    val readSamples = audioRecord?.read(audioBuffer, 0, audioBuffer.size) ?: 0
                    if (readSamples > 0) {
                        // Convert ShortArray to ByteArray (Little-Endian 16-bit PCM)
                        for (i in 0 until readSamples) {
                            byteBuffer[i * 2] = (audioBuffer[i].toInt() and 0x00FF).toByte()
                            byteBuffer[i * 2 + 1] = ((audioBuffer[i].toInt() shr 8) and 0x00FF).toByte()
                        }
                        
                        // Send PCM audio chunk to Python DSP daemon
                        val chunkPayload = JSONObject().apply {
                            put("timestamp_ms", System.currentTimeMillis())
                            put("sample_rate", SAMPLE_RATE)
                            put("samples_count", readSamples)
                            put("pcm_b64", Base64.encodeToString(byteBuffer, 0, readSamples * 2, Base64.NO_WRAP))
                        }
                        publishMqtt(TOPIC_AUDIO, chunkPayload.toString().toByteArray())
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Audio recording failed: ${e.message}", e)
            } finally {
                try {
                    audioRecord?.stop()
                    audioRecord?.release()
                } catch (ignored: Exception) {}
                audioRecord = null
                isRecording.set(false)
                Log.i(TAG, "Audio burst completed.")
            }
        }
    }

    private fun publishMqtt(topic: String, data: ByteArray) {
        try {
            if (mqttClient?.isConnected == true) {
                val message = MqttMessage(data).apply { qos = 0 } // QoS 0 for fast local loopback
                mqttClient?.publish(topic, message)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to publish MQTT message to $topic: ${e.message}")
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        isRecording.set(false)
        sensorManager.unregisterListener(this)
        wakeLock?.let { if (it.isHeld) it.release() }
        try {
            mqttClient?.disconnect()
            mqttClient?.close()
        } catch (ignored: Exception) {}
        executor.shutdown()
        audioExecutor.shutdown()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
