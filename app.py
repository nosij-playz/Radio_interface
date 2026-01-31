
from flask import Flask, render_template, request, redirect, session
from flask import jsonify
import requests
import sqlite3
from datetime import datetime, timedelta, timezone
import mysql.connector
from indic_transliteration import sanscript
from indic_transliteration.sanscript import transliterate
import threading
import time

from api_key import location_iq, owm, mysql as mysql_config

app = Flask(__name__)
app.secret_key = "secret123"  # change later

LOCATIONIQ_KEY = location_iq
OWM_KEY = owm


def _deg_to_compass(deg):
    if deg is None:
        return None
    try:
        d = float(deg)
    except (TypeError, ValueError):
        return None
    directions = [
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW",
    ]
    idx = int((d % 360) / 22.5 + 0.5) % 16
    return directions[idx]


def _fmt_local_time(epoch_seconds, tz_offset_seconds):
    if epoch_seconds is None or tz_offset_seconds is None:
        return None
    try:
        tz = timezone(timedelta(seconds=int(tz_offset_seconds)))
        return datetime.fromtimestamp(int(epoch_seconds), tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def get_db():
    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    return conn


def get_mysql():
    return mysql.connector.connect(**mysql_config)


def _pick_winner(votes, field, threshold, fallback_value):
    if not votes:
        return fallback_value
    top = votes[0]
    top_count = top.get("c") or 0
    if top_count < threshold:
        return fallback_value
    if len(votes) > 1 and (votes[1].get("c") == top_count):
        return fallback_value
    return top.get(field) or fallback_value


def _get_current_preference(window_minutes=30, threshold=5):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT language, COUNT(*) as c
        FROM polls
        WHERE created_at > datetime('now', ?)
        GROUP BY language
        ORDER BY c DESC
        """,
        (f"-{int(window_minutes)} minutes",),
    )
    lang_votes = [dict(row) for row in cur.fetchall()]

    cur.execute(
        """
        SELECT genre, COUNT(*) as c
        FROM polls
        WHERE created_at > datetime('now', ?)
        GROUP BY genre
        ORDER BY c DESC
        """,
        (f"-{int(window_minutes)} minutes",),
    )
    genre_votes = [dict(row) for row in cur.fetchall()]

    conn.close()

    language = _pick_winner(lang_votes, "language", threshold, "malayalam")
    genre = _pick_winner(genre_votes, "genre", threshold, "romantic")

    return {
        "language": language,
        "genre": genre,
        "lang_votes": lang_votes,
        "genre_votes": genre_votes,
        "window_minutes": window_minutes,
        "threshold": threshold,
    }

# -------------------- CREATE TABLES --------------------

with get_db() as db:
    # Users table (already exists)
    db.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT
    )
    """)

    # Location table (single row system config)
    db.execute("""
    CREATE TABLE IF NOT EXISTS location (
        id INTEGER PRIMARY KEY,
        place_name TEXT,
        latitude REAL,
        longitude REAL
    )
    """)

    # Polls table (SQLite)
    db.execute("""
    CREATE TABLE IF NOT EXISTS polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        language TEXT,
        genre TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

 # --- DAFETCH MODE GLOBAL ---
dafetch_mode = "online"  # default

# -------------------- DAFETCH MODE SETTER --------------------
@app.route("/monitor/dafetch_mode", methods=["POST"])
def monitor_set_dafetch_mode():
    if session.get("role") != "admin":
        return redirect("/monitor")

    mode = (request.form.get("dafetch_mode") or "").strip().lower()
    allowed = {"online", "offline", "mix"}
    if mode not in allowed:
        return redirect("/monitor")

    global dafetch_mode
    dafetch_mode = mode
    return redirect("/monitor")
# -------------------- AUTH --------------------

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form["username"]
        pwd = request.form["password"]

        # Hardcoded admin
        if user == "admin" and pwd == "admin":
            session["user"] = "admin"
            session["role"] = "admin"
            return redirect("/admin")

        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT * FROM users WHERE username=? AND password=?", (user, pwd))
        result = cur.fetchone()

        if result:
            session["user"] = result[1]
            session["role"] = result[3]
            return redirect("/dashboard")

        return "Invalid login"

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        user = request.form["username"]
        pwd = request.form["password"]

        try:
            db = get_db()
            db.execute("INSERT INTO users (username,password,role) VALUES (?,?,?)",
                       (user, pwd, "user"))
            db.commit()
            return redirect("/")
        except:
            return "Username already exists"

    return render_template("register.html")

# -------------------- DASHBOARDS --------------------

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/")
    current_pref = _get_current_preference()
    return render_template("dashboard.html", user=session["user"], current_pref=current_pref)


@app.route("/admin")
def admin():
    if session.get("role") != "admin":
        return redirect("/")

    db = get_db()
    users = db.execute("SELECT username, role FROM users").fetchall()

    # Location preview (SQLite)
    loc = db.execute("SELECT * FROM location WHERE id=1").fetchone()

    # Weather preview (OWM)
    weather = None
    if loc and loc[2] is not None and loc[3] is not None:
        lat = loc[2]
        lon = loc[3]
        url = (
            "https://api.openweathermap.org/data/2.5/weather"
            f"?lat={lat}&lon={lon}&appid={OWM_KEY}&units=metric"
        )
        try:
            res = requests.get(url, timeout=8)
            data = res.json()
            if res.ok and "main" in data and "weather" in data and data["weather"]:
                w0 = data["weather"][0] or {}
                main = data.get("main") or {}
                weather = {
                    "name": data.get("name"),
                    "desc": w0.get("description"),
                    "icon": w0.get("icon"),
                    "temp": main.get("temp"),
                    "feels": main.get("feels_like"),
                    "humidity": main.get("humidity"),
                }
        except Exception:
            weather = None
    current_pref = _get_current_preference()

    # Monitor preview (MySQL)
    status = None
    last_ai = None
    last_user = None
    try:
        conn = get_mysql()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM status_server WHERE id=1")
        status = cur.fetchone()
        cur.execute("SELECT * FROM ai_alert ORDER BY last_updated DESC LIMIT 1")
        last_ai = cur.fetchone()
        cur.execute("SELECT * FROM user_alert ORDER BY last_updated DESC LIMIT 1")
        last_user = cur.fetchone()
        conn.close()
    except Exception:
        status = None
        last_ai = None
        last_user = None

    # Get live vote results (grouped by language and genre, with counts)
    db2 = get_db()
    vote_results = db2.execute(
        """
        SELECT language, genre, COUNT(*) as count
        FROM polls
        WHERE created_at > datetime('now', ?)
        GROUP BY language, genre
        ORDER BY count DESC
        """,
        (f"-{current_pref['window_minutes']} minutes",)
    ).fetchall()
    db2.close()

    return render_template(
        "admin.html",
        users=users,
        loc=loc,
        weather=weather,
        status=status,
        last_ai=last_ai,
        last_user=last_user,
        current_pref=current_pref,
        vote_results=vote_results,
    )

@app.route("/vote", methods=["POST"])
def vote():
    language = (request.form.get("language") or "").strip().lower()
    genre = (request.form.get("genre") or "").strip().lower()

    allowed_languages = {"any", "english", "malayalam", "tamil"}
    allowed_genres = {"romantic", "chill", "happy", "sad", "energetic", "focus", "travel"}

    if language not in allowed_languages or genre not in allowed_genres:
        return jsonify({"status": "error", "message": "invalid vote"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO polls (language, genre) VALUES (?, ?)",
        (language, genre),
    )
    conn.commit()
    conn.close()

    if "text/html" in (request.headers.get("Accept") or ""):
        return redirect("/dashboard")

    return jsonify({"status": "ok"})

@app.route("/databack")
def databack():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM location WHERE id=1")
    loc = cur.fetchone()
    conn.close()

    current_pref = _get_current_preference()

    latitude = loc[2] if loc else None
    longitude = loc[3] if loc else None
    placename = loc[1] if loc else None

    global dafetch_mode
    return jsonify({
        "latitude": latitude,
        "longitude": longitude,
        "placename": placename,
        "language": current_pref["language"],
        "genre": current_pref["genre"],
        "dafetch_mode": dafetch_mode,
    })

# -------------------- LOCATION SYSTEM --------------------

@app.route("/location")
def location_page():
    if session.get("role") != "admin":
        return redirect("/")

    db = get_db()
    loc = db.execute("SELECT * FROM location WHERE id=1").fetchone()

    weather = None
    if loc and loc[2] is not None and loc[3] is not None:
        lat = loc[2]
        lon = loc[3]
        url = (
            "https://api.openweathermap.org/data/2.5/weather"
            f"?lat={lat}&lon={lon}&appid={OWM_KEY}&units=metric"
        )
        try:
            res = requests.get(url, timeout=8)
            data = res.json()

            if res.ok and "main" in data and "weather" in data and data["weather"]:
                tz_offset = data.get("timezone")
                main = data.get("main") or {}
                wind = data.get("wind") or {}
                sys = data.get("sys") or {}
                clouds = data.get("clouds") or {}
                rain = data.get("rain") or {}
                snow = data.get("snow") or {}
                coord = data.get("coord") or {}
                weather0 = data["weather"][0] or {}

                weather = {
                    # Identity / location
                    "name": data.get("name"),
                    "country": sys.get("country"),
                    "lat": coord.get("lat"),
                    "lon": coord.get("lon"),
                    "timezone_offset": tz_offset,
                    # Conditions
                    "condition": weather0.get("main"),
                    "desc": weather0.get("description"),
                    "icon": weather0.get("icon"),
                    # Temperature
                    "temp": main.get("temp"),
                    "feels": main.get("feels_like"),
                    "temp_min": main.get("temp_min"),
                    "temp_max": main.get("temp_max"),
                    # Atmosphere
                    "humidity": main.get("humidity"),
                    "pressure": main.get("pressure"),
                    "sea_level": main.get("sea_level"),
                    "ground_level": main.get("grnd_level"),
                    "visibility_m": data.get("visibility"),
                    # Wind / clouds / precip
                    "wind_speed": wind.get("speed"),
                    "wind_deg": wind.get("deg"),
                    "wind_dir": _deg_to_compass(wind.get("deg")),
                    "wind_gust": wind.get("gust"),
                    "cloudiness": clouds.get("all"),
                    "rain_1h": rain.get("1h"),
                    "rain_3h": rain.get("3h"),
                    "snow_1h": snow.get("1h"),
                    "snow_3h": snow.get("3h"),
                    # Sun / time
                    "observed_at": _fmt_local_time(data.get("dt"), tz_offset),
                    "sunrise": _fmt_local_time(sys.get("sunrise"), tz_offset),
                    "sunset": _fmt_local_time(sys.get("sunset"), tz_offset),
                }

                # Optional: Air Quality (AQI + pollutants)
                try:
                    aq_url = (
                        "https://api.openweathermap.org/data/2.5/air_pollution"
                        f"?lat={lat}&lon={lon}&appid={OWM_KEY}"
                    )
                    aq_res = requests.get(aq_url, timeout=8)
                    aq_data = aq_res.json()
                    if aq_res.ok and (aq_data.get("list") or []):
                        aqi0 = aq_data["list"][0] or {}
                        weather["air"] = {
                            "aqi": (aqi0.get("main") or {}).get("aqi"),
                            "components": aqi0.get("components") or {},
                            "observed_at": _fmt_local_time(aqi0.get("dt"), tz_offset),
                        }
                except Exception:
                    pass
        except Exception:
            weather = None

    return render_template(
        "location.html",
        loc=loc,
        weather=weather,
        locationiq_key=LOCATIONIQ_KEY,
    )

@app.route("/monitor")
def monitor():
    if session.get("role") != "admin":
        return redirect("/")

    conn = get_mysql()
    cur = conn.cursor(dictionary=True)

    # Status
    cur.execute("SELECT * FROM status_server WHERE id=1")
    status = cur.fetchone()

    # Music
    cur.execute("SELECT * FROM music WHERE id=1")
    music = cur.fetchone()

    # AI Alerts
    cur.execute("SELECT * FROM ai_alert ORDER BY last_updated DESC LIMIT 5")
    ai_alerts = cur.fetchall()

    # User Alerts
    cur.execute("SELECT * FROM user_alert ORDER BY last_updated DESC LIMIT 10")
    user_alerts = cur.fetchall()

    conn.close()

    global dafetch_mode
    return render_template(
        "monitor.html",
        status=status,
        music=music,
        ai_alerts=ai_alerts,
        user_alerts=user_alerts,
        dafetch_mode=dafetch_mode
    )

@app.route("/user_alert", methods=["GET", "POST"])
def user_alert_page():
    if session.get("role") != "admin":
        return redirect("/")

    conn = get_mysql()
    cur = conn.cursor(dictionary=True)

    if request.method == "POST":
        message = (request.form.get("message") or "").strip()
        language = (request.form.get("language") or "").strip()

        sender = session.get("user") or "admin"

        if message:
            # Store the message as typed. Preferred language is stored in 'source'.
            # If 'malayalam' is selected, message should be typed in Malayalam script.
            # If 'english' is selected, message should be typed in English.
            cur.execute(
                """
                INSERT INTO user_alert (id, user_id, message, last_updated)
                VALUES (1, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                    user_id=VALUES(user_id),
                    message=VALUES(message),
                    last_updated=VALUES(last_updated)
                """,
                (sender, message),
            )
            # Store the preferred language in the `source` column
            cur.execute(
                "UPDATE user_alert SET source=%s WHERE id=1",
                (language,),
            )
            conn.commit()

            # Start a thread to delete the alert after 5 seconds
            def delete_user_alert_after_delay():
                time.sleep(5)
                try:
                    conn2 = get_mysql()
                    cur2 = conn2.cursor()
                    cur2.execute("DELETE FROM user_alert WHERE id=1")
                    conn2.commit()
                    conn2.close()
                except Exception as e:
                    print(f"Error deleting user_alert: {e}")

            threading.Thread(target=delete_user_alert_after_delay, daemon=True).start()

    cur.execute("SELECT * FROM user_alert ORDER BY last_updated DESC LIMIT 20")
    alerts = cur.fetchall()
    conn.close()

    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')
    return render_template("user_alert.html", alerts=alerts, today=today)


@app.route("/monitor/status", methods=["POST"])
def monitor_set_status():
    if session.get("role") != "admin":
        return redirect("/")

    new_status = (request.form.get("status") or "").strip().lower()
    allowed = {"net", "freq", "both", "stop"}
    if new_status not in allowed:
        return redirect("/monitor")

    conn = get_mysql()
    cur = conn.cursor()
    # Ensure row exists (id=1), then update
    cur.execute(
        """
        INSERT INTO status_server (id, status, last_updated)
        VALUES (1, %s, NOW())
        ON DUPLICATE KEY UPDATE
            status=VALUES(status),
            last_updated=VALUES(last_updated)
        """,
        (new_status,),
    )
    conn.commit()
    conn.close()

    return redirect("/monitor")


@app.route("/monitor/ai_alert/delete", methods=["POST"])
def monitor_delete_ai_alert():
    if session.get("role") != "admin":
        return redirect("/")

    alert_id = request.form.get("id")
    try:
        alert_id_int = int(alert_id)
    except (TypeError, ValueError):
        return redirect("/monitor")

    conn = get_mysql()
    cur = conn.cursor()
    cur.execute("DELETE FROM ai_alert WHERE id=%s", (alert_id_int,))
    conn.commit()
    conn.close()
    return redirect("/monitor")


@app.route("/monitor/ai_alert/clear", methods=["POST"])
def monitor_clear_ai_alerts():
    if session.get("role") != "admin":
        return redirect("/")

    conn = get_mysql()
    cur = conn.cursor()
    cur.execute("DELETE FROM ai_alert")
    conn.commit()
    conn.close()
    return redirect("/monitor")


@app.route("/monitor/user_alert/delete", methods=["POST"])
def monitor_delete_user_alert():
    if session.get("role") != "admin":
        return redirect("/")

    alert_id = request.form.get("id")
    try:
        alert_id_int = int(alert_id)
    except (TypeError, ValueError):
        return redirect("/monitor")

    conn = get_mysql()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_alert WHERE id=%s", (alert_id_int,))
    conn.commit()
    conn.close()
    return redirect("/monitor")


@app.route("/monitor/user_alert/clear", methods=["POST"])
def monitor_clear_user_alerts():
    if session.get("role") != "admin":
        return redirect("/")

    conn = get_mysql()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_alert")
    conn.commit()
    conn.close()
    return redirect("/monitor")


@app.route("/save_location", methods=["POST"])
def save_location():
    if session.get("role") != "admin":
        return redirect("/")

    place = request.form["place_name"]
    lat = request.form["latitude"]
    lon = request.form["longitude"]

    db = get_db()
    existing = db.execute("SELECT * FROM location WHERE id=1").fetchone()

    if existing:
        db.execute("""
            UPDATE location
            SET place_name=?, latitude=?, longitude=?
            WHERE id=1
        """, (place, lat, lon))
    else:
        db.execute("""
            INSERT INTO location (id, place_name, latitude, longitude)
            VALUES (1, ?, ?, ?)
        """, (place, lat, lon))

    db.commit()
    return redirect("/location")

# -------------------- LOGOUT --------------------

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# -------------------- RUN --------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)

