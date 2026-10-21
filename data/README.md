# AirlineSim-Datenbanken

- `airlinesim.sqlite3`: bisherige Datenbank fuer Hindenburg V4, unveraendert.
  Sie bleibt als Archiv erhalten und wird nicht mehr standardmaessig verwendet.
- `hindenburg_v5.sqlite3`: aktive Standarddatenbank der Anwendung und der Jobs
  fuer Hindenburg V5.
  Die Flughafendaten stammen aus der aktuellen Hindenburg-Spielwelt.
  Sie enthaelt auch das Airline-Verzeichnis und die aktuellen Flugplaene.

Die Jobs koennen mit `--db-path` oder `AS_DB_PATH` weiterhin gezielt eine andere
Datenbank verwenden. Ohne diese Angabe werden neue Daten in V5 aktualisiert.

Die Tabelle `airports` enthaelt in `airport_name` den Namen aus der
Flughafen-Ueberschrift, ohne das Praefix und ohne den abschliessenden IATA-Code.
Bestehende Flughafentabellen erhalten diese Spalte beim naechsten Import;
vorhandene Daten und Koordinaten bleiben dabei erhalten.

Flughaefen fuer V5 neu einlesen oder aktualisieren:

```powershell
python jobs\scrape_airports.py --discover --db-path data\hindenburg_v5.sqlite3
```

`--discover` verwendet das Laender-Verzeichnis einschliesslich der Regionen.
Damit werden auch neue Flughaefen hinter groesseren Luecken im ID-Bereich erfasst.
Ohne diese Option wird weiterhin der mit `--start-id` und `--end-id` angegebene
Bereich verwendet.

Koordinaten aus OurAirports ergaenzen:

```powershell
python jobs\add_airport_coordinates.py --db-path data\hindenburg_v5.sqlite3
```

## Flugplaene und Schluessel

`airlines.enterprise_id` ist die Ingame-ID der Airline/Enterprise und der
Primaerschluessel des Airline-Verzeichnisses. Auch Holdings und Airlines ohne
Fluege werden gespeichert. Name, Quell-URL, Aktivstatus, letzter erfolgreicher
Flugplan-Abruf und letzter Fehler sind dort nachvollziehbar.

`flightplan_flights.id` ist eine interne, automatisch vergebene ID, nicht die
Flugnummer. Eine Flugnummer kann in mehreren Airlines, auf mehreren Legs und
als Via-Gesamtverbindung vorkommen. Alle diese Zeilen bleiben erhalten;
`is_via_summary = 1` kennzeichnet die Gesamtverbindung. Karten und Flugstatistiken
zaehlen weiterhin nur die echten Legs, nicht zusaetzlich die Via-Gesamtzeile.

Flughafendetails und Flugzeug-Popups zeigen Abflug und Ankunft in UTC mit
Wochentag. Die Flugzeug-Popups verwenden die berechneten Zeitpunkte
`departure_at` und `arrival_at`, nicht die lokalen Original-Flugplanzeiten.
Die gespeicherten lokalen Zeiten bleiben fuer die Berechnung unveraendert.

Die Zahl unter **Kartenebenen** bezeichnet die laut gespeichertem Flugplan
gerade unterwegs befindlichen, mit Koordinaten darstellbaren **Fluege**.
Sie ist keine Flottengroesse und keine Live-Meldung von AirlineSim; individuelle
Flugzeugkennungen und tatsaechliche Flugzustaende werden nicht importiert.
Verkehrstage und Gueltigkeitsgrenzen werden anhand des lokalen Abflugdatums
ausgewertet. Jahreslose Gueltigkeitsdaten werden relativ zum Importdatum
aufgeloest. Bei der Datumsgrenze wird die erste Ankunft nach dem Abflug
verwendet, damit nicht faelschlich ein zusaetzlicher Flugtag entsteht.
Angekommene Fluege werden auch zwischen den regelmaessigen Server-Abgleichen
aus der Animation und der angezeigten Anzahl entfernt.

Ein zusaetzlicher UNIQUE-Schluessel identifiziert einen konkreten Flugplaneintrag:
Airline-ID, Flugnummer, Start- und Zielflughafen, Abflug- und Ankunftszeit,
Verkehrstage, Gueltigkeit und Via-Kennzeichen. Die Schluesselfelder sind nicht
NULL. Dadurch koennen etwa unterschiedliche Verkehrstage oder Gueltigkeiten
nicht versehentlich dieselbe Zeile ueberschreiben.

Bei unveraendertem Schluessel bleibt die interne ID auch nach einem Update
erhalten. Aendern sich beispielsweise Zeiten oder Verkehrstage, entsteht ein
neuer Flugplaneintrag; der alte wird beim Synchronisieren entfernt. Die ID
bezeichnet somit einen konkreten Flugplaneintrag, keine dauerhaft identische
Flugnummer oder physische Maschine.

## Vollstaendige und einzelne Updates

In der App kann der Flugplan der ausgewaehlten **primaeren Airline** mit
**Airline aktualisieren** neu eingelesen werden. Bei leerer Auswahl oder
**Alle Airlines** ist der Button deaktiviert. Eine Vergleichs-Airline wird nicht
mit aktualisiert.

Das Update laeuft im Hintergrund. Waehrenddessen sind die Airline-Auswahl und
der Button gesperrt; der Status wird angezeigt. Nach erfolgreichem Abschluss
werden die Airline-Liste samt Flugzahlen, Karte, Kennzahlen und offene
Flughafendetails fuer dieselbe Auswahl neu geladen. Fehler werden sichtbar
gemeldet; bei einem fehlgeschlagenen Import bleibt der bisherige Flugplan
erhalten. Ein Verbindungsabbruch oder das Schliessen der Browserseite bricht
den serverseitigen Import nicht ab. Ein Neustart des App-Prozesses kann einen
laufenden Hintergrundjob unterbrechen; die vorhandene Import-Sperre behandelt
solche verwaisten Laeufe nach ihrem Ablauf wie beim CLI-Import.

Der UI-Import verwendet dieselbe Datenbank-Sperre wie die Kommandozeilen-Jobs:
Ein bereits laufender Import verhindert weitere Starts, auch fuer andere
Airlines. Start-Anfragen erfolgen per POST mit dem Anwendungs-Header
`X-Requested-With: XMLHttpRequest`; der Status wird fuer die konkrete Lauf-ID
abgefragt, nicht nur fuer den zuletzt gestarteten Import.

Alle Airlines samt Verzeichnis aktualisieren:

```powershell
python jobs\refresh_flightplans.py --db-path data\hindenburg_v5.sqlite3
```

Nur eine Airline anhand ihrer Ingame-ID aktualisieren, ohne andere anzutasten:

```powershell
python jobs\refresh_flightplans.py --db-path data\hindenburg_v5.sqlite3 --enterprise-id 80
```

`--enterprise-id` ist fuer mehrere gezielte Airline-Updates wiederholbar.
Alternativ unterstuetzt `jobs\scrape_flightplan.py` die Auswahl mit
`--all-enterprises` oder `--enterprise-id 80`. Ohne explizite Auswahl startet
dieses Skript keinen Import.

Standardmaessig wird jeder erfolgreich abgerufene Airline-Flugplan als
vollstaendiger Snapshot synchronisiert: neue Eintraege hinzufuegen, bestehende
aktualisieren und entfallene entfernen. Das geschieht pro Airline in einer
einzigen Transaktion. Ein ausdruecklich leerer Flugplan entfernt die bisherigen
Fluege, behaelt aber die Airline. Fehlgeschlagene Abrufe, unvollstaendige Zeilen,
unbekannte Seiten und widerspruechliche Daten ersetzen niemals den letzten
erfolgreichen Stand; der Fehler wird in `airlines.last_error` und im Importlauf
festgehalten. Andere erfolgreich aktualisierte Airlines bleiben erhalten.

Beim vollstaendigen Update werden nicht mehr im Verzeichnis vorhandene Airlines
inaktiv markiert und ihre aktuellen Fluege entfernt. Der Airline-Datensatz bleibt
zur Einordnung erhalten. Ein einzelnes Airline-Update gleicht das gesamte
Verzeichnis nicht ab.

Beide Einstiegspunkte verwenden die Import-Sperre und protokollieren Laeufe in
`import_runs`. HTTP-Rate-Limits und voruebergehende Serverfehler werden mit
begrenzten Wiederholungen und Wartezeiten behandelt. Ein teilweise
fehlgeschlagener Import wird ausdruecklich als Fehler gemeldet.

Nur fuer bewusst additive Einzelimporte gibt es bei `scrape_flightplan.py`
`--append`: Dabei werden entfallene Eintraege **nicht** entfernt. Fuer einen
konsistenten aktuellen Datenbestand diese Option nicht verwenden.
