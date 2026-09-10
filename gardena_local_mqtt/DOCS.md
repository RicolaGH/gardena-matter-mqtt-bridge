# GARDENA Local MQTT 0.2.0 — Gateway-Betrieb

Die MQTT-Software läuft als eigener Dienst **auf dem GARDENA smart Gateway (19005)**.
Die Home-Assistant-App übernimmt Einrichtung, Updates und Diagnose. Nach erfolgreicher
Installation darf sie gestoppt werden: Sensoren und Mähersteuerung funktionieren weiter.
Der MQTT-Broker muss erreichbar bleiben. Läuft Mosquitto in HA, muss natürlich der
Broker selbst weiterlaufen; nur diese GARDENA-App wird nicht mehr benötigt.

Version 0.1.x hatte den MQTT-Prozess fälschlich in die HA-App verlagert. Dieses Update
korrigiert die Architektur. Ein App-Update allein verschiebt den Prozess noch nicht:
Die unten beschriebene Gateway-Installation muss einmal ausgeführt werden.

## Wechsel von 0.1.1

1. Backup der App behalten und auf 0.2.0 aktualisieren. Die bisherigen Einstellungen,
   SSH-Schlüssel, Sensorzuordnungen und Übernahmedaten bleiben in `/data` erhalten.
2. App starten und die Benutzeroberfläche öffnen. Bis zur Umschaltung übernimmt der
   bisherige HA-Prozess weiterhin MQTT, wenn bereits eine aktive Übernahme vorliegt.
3. **Auf Gateway installieren** auswählen. Es werden freier Speicher, Prüfsumme,
   Sensorwerte und die lokale WebSocket-API geprüft. Dabei wird kein Mähbefehl gesendet.
4. Erst nach erfolgreicher Vorprüfung stoppt der Installer den HA-MQTT-Prozess und
   startet/aktiviert `gardena-local.service` auf dem Gateway. Ein Journal verhindert
   bei unklarem Ergebnis, dass zusätzlich wieder der HA-Controller gestartet wird.
5. Warten, bis **MQTT läuft auf dem Gateway. Diese HA-App kann gestoppt werden.** erscheint.
6. **Diese GARDENA-App stoppen.** Nach mindestens zwei Minuten Sensorverfügbarkeit und
   einen Parkbefehl in HA prüfen. Ein sicher beaufsichtigter Start/Rückkehr-Test kann folgen.
7. Den Gateway-Neustart und einen Broker-Neustart separat testen. Der Gateway-Dienst
   startet selbstständig und verbindet sich erneut; die HA-App bleibt dabei gestoppt.

Der tatsächliche Gateway-Test für 0.2.0 steht noch aus. Containerbau und MIPS-Emulation
ersetzen keine Prüfung von Speicherverbrauch, Gateway-Neustart und Laufzeit auf Hardware.

## Neue Installation

Gateway-Adresse, Geräte-ID und MQTT-Zugangsdaten eintragen. Die ersten acht Zeichen der
Geräte-ID sind das Gateway-Anmeldepasswort. Zunächst die Sensorvorschau prüfen und
**Sensoren und Steuerung übernehmen** auswählen; bei bestehenden Installationen vorher
andere HA-Controller stoppen. Danach **Auf Gateway installieren** ausführen.
Die erste Version unterstützt weiterhin die vorhandene eindeutige Ein-Mäher-Zuordnung.
Bei unbekannten Sensortypen oder mehrdeutigen Identitäten wird die Migration blockiert.

## Was auf dem Gateway installiert wird

- Ein selbst gebautes, statisches Linux/MIPS-little-endian-Programm mit Software-Floating-Point.
- Programm und Konfiguration unter `/usr/local/lib/gardena-local/releases/`, ein `current`-Link
  und `/etc/systemd/system/gardena-local.service` mit `Restart=always` und Autostart.
- Sensorwerte werden direkt aus `/var/lib/lemonbeatd` gelesen. Steuerbefehle gehen an
  die lokale Gateway-WebSocket-API auf Loopback. Es gibt keinen HA-SSH-Tunnel im Betrieb.
- MQTT-Zugangsdaten stehen ausschließlich in einer privaten Konfigurationsdatei (0600),
  nicht in Prozessargumenten. Der öffentliche SSH-Schlüssel dient nur der Verwaltung.
- Keine Downloads aus dem Original-Bridge-Repository, keine Abhängigkeit vom bisherigen
  Gateway-Publisher oder dessen Matter-/Toggle-Weboberfläche. Die Basis ist weiterhin
  die Hersteller-Firmware mit ihren Geräte- und lokalen API-Diensten.

Die Gateway-Binary wird beim App-Containerbau aus `gateway_runtime/` erzeugt und im
App-Image mitgeliefert. Der Build benötigt das Go- und HA-Basisimage sowie Alpine-Pakete.
Das Gateway selbst lädt weder Code noch Bibliotheken aus dem Internet.

## Status, Abbruch und Rückkehr

Bei installiertem Gateway-Dienst fragt die HA-App nur den Dienststatus ab. Das Stoppen
oder Deinstallieren der HA-App stoppt den Gateway-Dienst nicht und sendet kein MQTT-offline.
Bei Broker-/API-Ausfällen setzt der Gateway-Dienst die Verfügbarkeit auf offline und
versucht die Verbindung erneut. Gespeicherte MQTT-Befehle werden nicht ausgeführt.

Vorprüfung fehlgeschlagen: Der bisherige HA-Prozess bleibt aktiv. Nach fehlgeschlagener
Umschaltung darf er erst wieder laufen, wenn der neue Gateway-Dienst sicher gestoppt
wurde. Bei fehlender SSH-Verbindung bleibt das Umschaltjournal deshalb bestehen.
**HA-Betrieb wiederherstellen** stoppt/deaktiviert den neuen Gateway-Dienst und startet
anschließend den bisherigen Prozess in der App. Das ist eine Rückfalloption, kein
notwendiger Teil des normalen Betriebs. Alte 0.1.x-Backups nicht parallel zu einem
laufenden Gateway-Dienst starten.

Vorhandene Matter-Dateien und Vendor-Dienste werden nicht gelöscht. Bei Platzmangel
bricht die Vorbereitung ab; ein gezieltes Aufräumen erfolgt erst nach Prüfung.
OTA-Firmwareupdates können das Overlay verändern. Gateway-Reboot ist Teil des Tests;
Unverwundbarkeit gegenüber beliebigen Hersteller-OTA-Updates wird nicht zugesichert.

## Zurückgestellte Sicherheitsthemen

MQTT-TLS und unabhängige Prüfung des Gateway-HTTPS-Zertifikats bleiben zurückgestellt.
SSH speichert Hostkeys dauerhaft und lehnt geänderte Schlüssel ab; erster Kontakt ist
TOFU. Die runtimeeigene lokale API-Verbindung verwendet ausschließlich Loopback.
Die Broker-Discovery ist kein kryptografischer Identitätsnachweis. Der Broker und seine
Publikationsrechte müssen vertrauenswürdig sein.

## Entwicklung und Prüfungen

- `python3 -m unittest discover -s gardena_local_mqtt/tests -v`
- `cd gardena_local_mqtt/gateway_runtime && go test ./...`
- Cross-Build: `CGO_ENABLED=0 GOOS=linux GOARCH=mipsle GOMIPS=softfloat go build -trimpath -ldflags="-s -w" .`
- CI: native amd64/aarch64-App-Container, Gateway-Protokolltests ohne HA-Installer,
  Integrationsprüfung mit simuliertem Broker und lokaler API, MIPS-Ausführung unter QEMU.

Die Urheber- und Lizenzhinweise stehen in LICENSE und NOTICE. LsDL-Format und bisherige
Mäherbefehle stammen aus den dokumentierten Schnittstellen und dem getesteten Fork-Code.
