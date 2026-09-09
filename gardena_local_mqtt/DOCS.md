# GARDENA Local MQTT — Testversion 0.1.1

Diese eigene Home-Assistant-App liest Sensorwerte und steuert Mäher lokal über
das GARDENA smart Gateway. Sie lädt keine Releases aus dem Originalprojekt,
installiert keine Gateway-Binaries und benötigt keinen Matter-/Toggle-Dienst.
Gateway-Firmware, SSH, lokale WebSocket-API und ein MQTT-Broker bleiben erforderlich.
Beim Containerbau werden weiterhin das HA-Basisimage und Alpine-Pakete geladen.

## Einrichtung und Vorschau

1. Die App **GARDENA Local MQTT (Preview)** aus diesem Repository installieren.
2. Gateway-IP, Geräte-ID sowie die bisherigen MQTT-Einstellungen übernehmen.
   Die Geräte-ID bleibt geheim; die ersten acht Zeichen dienen der Gateway-Anmeldung.
3. App starten und **Benutzeroberfläche öffnen** wählen.
4. Die Vorschau zeigt die Zahl der zugeordneten Sensoren und Mäher. Sie veröffentlicht
   keine Sensor-Discovery und akzeptiert keine Steuerbefehle. Der alte Gateway-Publisher
   läuft bis zur Übernahme weiter.

Schon die Vorbereitung meldet sich lokal am Gateway an, hinterlegt bei Bedarf den
öffentlichen SSH-Schlüssel dieser App und aktiviert SSH sowie die lokale WebSocket-API.
Ein eigener persistenter Hostkey-Speicher lehnt spätere Schlüsselwechsel ab. Der erste
Kontakt bleibt TOFU. SSH-/API-Einstellungen können auch bestehende Gateway-Apps betreffen.

## Übernahme

1. Backup der bisherigen Bridge-App behalten.
2. In der bisherigen **HA-Bridge-App** Autostart und Watchdog ausschalten und sie stoppen.
   Nur eine App darf die Mäherbefehle verarbeiten.
3. In der neuen Oberfläche **Sensoren und Steuerung übernehmen** auswählen.
4. Die App prüft die vorhandenen Sensor-Discovery-Einträge erneut und beendet/deaktiviert
   anschließend `gardena-mqtt-publisher.service` auf dem Gateway. Dateien bleiben erhalten.
5. Home Assistant: Gerätezuordnung, Sensorwerte und einen Parkbefehl prüfen. Danach die
   neue App neu starten und die erneute Verbindung prüfen. Erst dann Autostart einschalten.

Vorhandene Sensor-Discovery-Topics, `unique_id`, Zustandstopics und Gerätekennungen bleiben
erhalten. Die Verfügbarkeit wechselt auf `<mqtt_topic_prefix>/local/availability`.
Sensordaten werden alle 30 Sekunden gelesen; Mäheraktivität wird über WebSocket aktualisiert.
Gespeicherte MQTT-Befehle werden niemals nachträglich ausgeführt. Ohne Gateway-Verbindung
meldet die App ihre Entitäten offline und versucht die Verbindung erneut.

Die erste Migration unterstützt **ein eindeutig zugeordnetes LsDL-Gerät und einen Mäher**.
Bei mehreren Geräten/Publisher-Identitäten oder fehlenden/unerkannten Sensorwerten wird
die Übernahme blockiert. Neu eingerichtete Sensor-Gateways ohne Mäher können mehrere
Geräte mit eigenen stabilen Kennungen veröffentlichen. Mäher ohne lesbare LsDL-Sensoren
(möglicherweise neuere Generationen) benötigen einen zusätzlichen Sensoradapter.
Die bekannten Mäherbefehle sind übernommen, deren Unterstützung allein garantiert noch
keine vollständige Sensorunterstützung für jede Generation.

## Zurückkehren

In der neuen Oberfläche **Zum bisherigen Publisher zurückkehren** wählen. Die App
stellt die vorherigen Sensor-Discovery-Einträge und den vorherigen Aktivierungszustand
des Gateway-Publishers wieder her und beendet ihren Worker. Anschließend diese App
stoppen und die alte HA-App wieder starten. Ihre Mäher-Discovery wird beim Start erneuert.
Falls deren SSH-Schlüssel nicht mehr akzeptiert wird, den Einrichtungs-/Deploy-Vorgang
der alten App verwenden. Nach der Rückkehr die alte Geräteanzeige und Steuerung prüfen.

Ein privates, atomar geschriebenes Migrationsjournal erlaubt die Wiederherstellung nach
einem Abbruch. Bei Änderungen an Gateway, Broker oder Topic-Präfixen während einer
aktiven Migration müssen zuerst die bisherigen Einstellungen wieder eingesetzt werden.
Die Rückkehr benötigt weiterhin eine erreichbare Gateway- und Broker-Verbindung.

## Umfang und Grenzen

- Unterstützte LsDL-Ressourcen: Batterie, Mäherstatus, Funkqualität, Lauf-/Mähzeit,
  Fehlercode, Bodenfeuchte/-temperatur, Licht, Frostwarnung und Messintervall, sofern vorhanden.
- Lokale API und SSH reichen aus; keine eingehenden HA-Ports, kein Host-Netzwerk,
  keine Supervisor-Administrationsrechte. Oberfläche nur über HA Ingress.
- Matter wird weder installiert noch verwaltet. Bestehende Matter-Dateien/Dienste
  werden nicht entfernt; ihre Funktion nach Änderungen der lokalen API ist separat zu prüfen.
- MQTT-TLS und Gateway-Zertifikatsprüfung sind die zurückgestellte Sicherheitsfolgearbeit.
- Broker-Discovery ist keine kryptografisch gesicherte Gerätezuordnung. Nur für einen
  vertrauenswürdigen Broker mit kontrollierten Veröffentlichungsrechten verwenden.
- Die Snapshot-Größe ist auf 8 MiB, einzelne JSON-Dateien auf 64 KiB und die Dateizahl
  auf 4096 begrenzt. Archivdateien werden nicht ins Dateisystem entpackt.
- Diese Version ist noch nicht auf einem echten Gateway getestet. Die Tests verwenden
  synthetische LsDL-Daten nach dem dokumentierten Format. Der Vorschaulauf auf echter
  Hardware muss insbesondere die tatsächlichen Sensor-Schemas bestätigen.

## Entwicklung

`python3 -m unittest discover -s gardena_local_mqtt/tests -v`

Der Workflow `Local MQTT app` prüft Tests und Containerbau auf amd64 und aarch64.
Der gesamte Build-Kontext liegt in `gardena_local_mqtt/`; die bisherige App und der
Windows-Installer sind keine Laufzeit- oder Build-Abhängigkeiten.

Formatreferenz für LsDL: `technical.md` und `mqtt.md` im bestehenden Repository.
HA-App-Konfiguration: https://developers.home-assistant.io/docs/apps/configuration/
Der Transport-/Mähercode ist eine angepasste Kopie des getesteten Fork-Stands 0.3.2;
Urheber- und Lizenzhinweise bleiben in LICENSE und NOTICE enthalten.
