# 0.1.1

- Versionsnummer zur Unterscheidung der regulären App von der Testinstallation erhöht.
- Keine funktionalen Änderungen gegenüber 0.1.0.

# 0.1.0

- Separate HA-App mit lokalem LsDL-Sensorleser und MQTT-Mähersteuerung.
- Eigenständige SSH-/WebSocket-Einrichtung ohne Gateway-Bridge-Download.
- Ingress-Vorschau, geprüfte Sensorübernahme und Wiederherstellungsjournal.
- Bestehende MQTT-Sensoridentitäten bleiben bei eindeutiger Zuordnung erhalten.
- Container-Builds für amd64/aarch64 und isolierte Regressionstests.
