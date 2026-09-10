## 0.2.1

- Explizite Gateway-Bereinigung für alte Matter-Dienste und Webseite.
- Vorherige MQTT-Zustandsprüfung, feste Pfadliste und private Sicherung statt endgültigem Löschen.
- Gateway-MQTT-Programm unverändert (Runtime-Version 0.2.0).

# 0.2.0

- MQTT-Sensoren und Mähersteuerung laufen nach Installation als eigener Gateway-Dienst.
- HA-App nur noch für Einrichtung, Updates und Status nötig.
- Eigenes statisches MIPS-Programm, systemd-Autostart und selbstständiger Wiederanlauf.
- Geprüfte Umschaltung mit unveränderten Entitäten und Rückfall auf bisherigen HA-Betrieb.
- Geheimnisse per privater Datei statt Prozessargumenten.

# 0.1.1

- Versionsnummer zur Unterscheidung der regulären App erhöht; keine funktionalen Änderungen.

# 0.1.0

- Separate HA-App mit lokalem LsDL-Sensorleser und MQTT-Mähersteuerung.
- Eigenständige SSH-/WebSocket-Einrichtung ohne Gateway-Bridge-Download.
- Ingress-Vorschau, geprüfte Sensorübernahme und Wiederherstellungsjournal.
- Bestehende MQTT-Sensoridentitäten bleiben bei eindeutiger Zuordnung erhalten.
- Container-Builds für amd64/aarch64 und isolierte Regressionstests.
