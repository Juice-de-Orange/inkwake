# Data sources

inkwake fills four panels. Two need no configuration at all (weather, sun times),
the other two read from sources you point it at. Every source is allowed to fail:
a dead source costs its panel, never the frame, and the footer says which part is
stale.

| Panel | Source | Configured by |
|---|---|---|
| Weather | [Open-Meteo](https://open-meteo.com/), no key needed | `WEATHER_LAT`, `WEATHER_LON` |
| Sunrise / sunset | computed locally, no network | same coordinates |
| Events | any number of iCalendar (ICS) feeds | `EVENTS_ICS_URLS` |
| Bookings, sighting | two Postgres views, optional | `SOURCES_DSN`, `BOOKINGS_VIEW`, `SIGHTINGS_VIEW` |

## Events: ICS feeds

Set `EVENTS_ICS_URLS` in `.env` to one or more calendar URLs, separated by commas:

```dotenv
EVENTS_ICS_URLS=https://calendar.example.org/club.ics,https://cloud.example.com/remote.php/dav/public-calendars/abc123?export
EVENTS_COUNT=6          # rows on the panel
EVENTS_DAYS_AHEAD=7     # how far ahead to look
EVENTS_TITLE=Termine    # the section heading
```

Anything that serves `text/calendar` works: a public club or venue calendar, the
"secret address in iCal format" of a Google calendar, a Nextcloud or Radicale
export link. What is supported:

- timed and all-day events, time zones (converted to `TZ_NAME`) and floating times;
- recurring events (`RRULE`) with exceptions (`EXDATE`) and moved or cancelled
  single occurrences (`RECURRENCE-ID`);
- multi-day events: every day they touch is shown, the first with its start time,
  later days as "still going";
- cancelled events (`STATUS:CANCELLED`) are left out.

On top of that, two noise filters apply: the same event in two calendars
("Summer Concert" and "summer concert ") is shown once, and an event that spans
more than two days in the window (a festival, a daily market) collapses to a
single row on its next day, so it does not fill the panel.

Each feed's last good answer is cached in the data volume for up to seven days.
When a feed is down the panel keeps showing the cached events; the footer admits
it only when *every* calendar is down, so a single flaky feed does not teach
people to ignore the hint.

> **A private calendar link is a password.** Keep it in `.env` only. inkwake never
> logs it and names its cache files after a hash of the URL, not the URL.

## Bookings and sightings: two Postgres views

Both panels are optional and come from the same database. Leave `SOURCES_DSN`
empty and both sections disappear from the frame without a failure.

The contract is two **views** with fixed column names. Whatever your booking tool
or camera pipeline stores, a view maps it onto these columns, so inkwake never
needs to know your schema — and you decide in the view which rows count
(confirmed bookings only, published sightings only, …).

### `inkwake_bookings`

| Column | Type | Meaning |
|---|---|---|
| `arrival_date` | `date` | first night |
| `departure_date` | `date` | the day the place is free again (hotel convention) |
| `name` | `text` | what the panel prints for the booking |
| `guest_count` | `integer` | number of guests |
| `comment` | `text` | optional, may be `NULL` |

inkwake shows the next `BOOKINGS_COUNT` stays whose `departure_date` is after
today, ordered by arrival. "Today" is computed in `TZ_NAME` and passed in as a
parameter, so a database running in UTC does not bring back last night's stay
between midnight and 02:00.

### `inkwake_sightings`

| Column | Type | Meaning |
|---|---|---|
| `captured_at` | `timestamptz` | when the picture was taken |
| `caption` | `text` | printed next to the photo |
| `species` | `text` | may be empty |
| `individual` | `text` | may be empty (a name for a recurring animal) |
| `camera` | `text` | may be empty |
| `image_path` | `text` | path relative to the sightings directory, may be `NULL` |

inkwake shows the newest row. `image_path` is resolved inside the directory
mounted at `/sightings` (`SIGHTINGS_HOST_DIR` in `.env`); a path that escapes it
or a file that is missing costs the picture, not the caption.

### Example

```sql
-- The views run with their owner's rights, so the reader role needs SELECT on
-- the views only, not on your tables.
CREATE VIEW inkwake_bookings AS
SELECT b.check_in                        AS arrival_date,
       b.check_out                       AS departure_date,
       coalesce(b.display_name, b.guest) AS name,
       b.guests                          AS guest_count,
       b.note                            AS comment
  FROM bookings b
 WHERE b.status = 'confirmed';

CREATE VIEW inkwake_sightings AS
SELECT s.taken_at      AS captured_at,
       s.description   AS caption,
       s.species       AS species,
       s.nickname      AS individual,
       c.name          AS camera,
       s.file          AS image_path
  FROM camera_sightings s
  JOIN cameras c ON c.id = s.camera_id
 WHERE s.published;

CREATE ROLE inkwake_reader LOGIN PASSWORD 'change-me';
GRANT SELECT ON inkwake_bookings, inkwake_sightings TO inkwake_reader;
```

```dotenv
SOURCES_DSN=postgresql://inkwake_reader:change-me@db.example.com:5432/app?sslmode=require
BOOKINGS_TITLE=Buchungen
SIGHTING_TITLE=Sichtung
SIGHTINGS_HOST_DIR=/srv/camera/pictures
```

inkwake opens every session read-only with a five-second statement timeout, and
view names from the configuration are quoted as identifiers, never interpolated.
Still, give it a role that can read these two views and nothing else.

> **Personal data.** A frame can carry the names from the bookings view. Set
> `ADMIN_API_KEY`, because `/preview` renders a frame on demand, and keep the
> server off the open internet unless it sits behind a reverse proxy with TLS.
