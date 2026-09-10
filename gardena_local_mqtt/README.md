# GARDENA Local MQTT

Installiert einen eigenen MQTT-Dienst auf dem GARDENA smart Gateway (19005).
**Nach erfolgreicher Installation wird diese HA-App für den Betrieb nicht benötigt.**
Sensoren und Mähersteuerung laufen auf dem Gateway weiter, solange der MQTT-Broker
und das lokale Netzwerk erreichbar sind.

Version 0.2.0 korrigiert die HA-Laufzeitarchitektur von 0.1.x. Nach dem App-Update
muss einmal **Auf Gateway installieren** ausgeführt werden.

Einrichtung, Test ohne laufende HA-App und Rückfalloption: [DOCS.md](DOCS.md).
