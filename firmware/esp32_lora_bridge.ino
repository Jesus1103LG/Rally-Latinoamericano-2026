/*
 * ==============================================================================
 * AgroMesh Level 2 - Hardware LoRa Transceiver Bridge Firmware
 * Target: ESP32 + Semtech SX1262 / SX1276 (LilyGO T-Beam, Heltec LoRa 32 V3)
 * Function: Bridges UART Serial (USB-OTG to Android Phone) <-> Sub-GHz LoRa RF
 * ==============================================================================
 *
 * Protocol: SLIP framing delimiter (0xC0) wrapping 27-byte AgroMesh binary frames.
 * Radio Config: 915.0 MHz (Americas) or 868.0 MHz (Europe), BW 125kHz, SF7, CR 4/5.
 */

#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>

// Frequency Band Selection (Set according to regional regulations)
#define RF_FREQUENCY          915.0   // MHz (Americas: 915.0, Europe: 868.0)
#define RF_BANDWIDTH          125.0   // kHz
#define RF_SPREADING_FACTOR   7       // SF7 (Fast ToA ~56ms, high immunity)
#define RF_CODING_RATE        5       // 4/5
#define RF_SYNC_WORD          0x12    // AgroMesh Private Sync Word
#define RF_OUTPUT_POWER       22      // dBm (max range)

// SLIP Framing delimiter
#define SLIP_END              0xC0

// SX1262 Pin Definition (Example: Heltec V3 / LilyGO T-Beam)
#if defined(HELTEC_V3)
  SX1262 radio = new Module(8, 14, 12, 13);
#elif defined(T_BEAM)
  SX1276 radio = new Module(18, 26, 23, 33);
#else
  // Generic ESP32 SPI LoRa pins
  SX1262 radio = new Module(5, 2, 14, 4);
#endif

// Packet buffers
#define FRAME_SIZE 27
uint8_t tx_buffer[FRAME_SIZE];
uint8_t rx_buffer[FRAME_SIZE];
volatile bool received_flag = false;

// Interrupt Service Routine for packet reception
void setFlag(void) {
  received_flag = true;
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000);

  // Initialize RadioLib
  int state = radio.begin(RF_FREQUENCY, RF_BANDWIDTH, RF_SPREADING_FACTOR, RF_CODING_RATE, RF_SYNC_WORD, RF_OUTPUT_POWER);
  if (state != RADIOLIB_ERR_NONE) {
    // Flash LED or error report over Serial
    while (true) {
      delay(1000);
    }
  }

  // Set packet received callback
  radio.setPacketReceivedAction(setFlag);

  // Start continuous listening
  radio.startReceive();
}

void loop() {
  // 1. Process Outbound Frame from Smartphone (UART -> LoRa Radio)
  if (Serial.available() >= (FRAME_SIZE + 2)) {
    int first_byte = Serial.read();
    if (first_byte == SLIP_END) {
      size_t bytes_read = Serial.readBytes(tx_buffer, FRAME_SIZE);
      int end_byte = Serial.read();

      if (bytes_read == FRAME_SIZE && end_byte == SLIP_END) {
        // Transmit over LoRa with Channel Activity Detection (CAD)
        radio.standby();
        radio.transmit(tx_buffer, FRAME_SIZE);
        radio.startReceive(); // Return to listening mode
      }
    }
  }

  // 2. Process Inbound Frame from Remote Master (LoRa Radio -> UART)
  if (received_flag) {
    received_flag = false;
    int state = radio.readData(rx_buffer, FRAME_SIZE);

    if (state == RADIOLIB_ERR_NONE) {
      // Forward frame to Smartphone via SLIP over USB-OTG Serial
      Serial.write(SLIP_END);
      Serial.write(rx_buffer, FRAME_SIZE);
      Serial.write(SLIP_END);
      Serial.flush();
    }

    radio.startReceive();
  }
}
