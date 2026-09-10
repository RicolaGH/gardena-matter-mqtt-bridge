# GARDENA Local MQTT

Sensoren und Mähersteuerung über MQTT direkt auf dem GARDENA smart Gateway (19005).
Die Home-Assistant-App installiert und verwaltet den Gateway-Dienst. Nach erfolgreicher
Installation kann die App gestoppt bleiben; der MQTT-Broker muss weiterlaufen.

## Installation und Betrieb

Dieses Repository als App-Repository in Home Assistant hinzufügen:
`https://github.com/RicolaGH/gardena-matter-mqtt-bridge`

Die App **GARDENA Local MQTT** installieren und konfigurieren. Sensoren übernehmen,
anschließend **Auf Gateway installieren** ausführen. Erst nach bestätigtem Gateway-Betrieb
die App stoppen. Details zu Einrichtung, Updates und Rückkehr zum HA-Betrieb stehen in
[der Anleitung](gardena_local_mqtt/DOCS.md).

## Umfang und Abhängigkeiten

- Gateway-Programm und HA-Installer werden aus diesem Repository gebaut.
- Der laufende Dienst benötigt die Hersteller-Firmware, deren lokale API und den MQTT-Broker.
- Zum Bauen werden Go, das HA-Basisimage und Alpine-Pakete benötigt.
- Keine Matter-Bridge, Matter-Release-Downloads oder alten Desktop-Installer enthalten.
- Frühere Matter-Versionen bleiben in der Git-Historie nachvollziehbar.
- Die Bereinigung des Repositorys entfernt keine bereits installierten Dateien auf einem Gateway.

## Sicherheit

MQTT wird derzeit ohne TLS übertragen. Der Installer prüft das Gateway-HTTPS-Zertifikat
noch nicht unabhängig; SSH vertraut beim ersten Kontakt einem neuen Schlüssel und lehnt
spätere Schlüsseländerungen ab. Broker-Zugriffe entsprechend einschränken.
Weitere Grenzen sind in der [Anleitung](gardena_local_mqtt/DOCS.md) dokumentiert.

## Entwicklung

```sh
python3 -m unittest discover -s gardena_local_mqtt/tests -v
cd gardena_local_mqtt/gateway_runtime
go test ./...
```

GitHub Actions prüft Python- und Go-Tests, beide App-Architekturen und die Ausführung der
Gateway-Binary unter MIPS-Emulation. Siehe [Workflow](.github/workflows/local-mqtt.yml).

Lizenz und ursprüngliche Urheberhinweise: [LICENSE](LICENSE), [NOTICE](NOTICE).
