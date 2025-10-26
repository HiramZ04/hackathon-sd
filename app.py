import os
import io
import time
import math
import uuid
import sqlite3
from datetime import datetime
from io import BytesIO

import pandas as pd
import streamlit as st
import pydeck as pdk
import requests
from fpdf import FPDF
import qrcode
from PIL import Image
from dotenv import load_dotenv

# -----------------------------
# Config inicial
# -----------------------------
st.set_page_config(page_title="OpenHealth SD - MVP", page_icon="🧭", layout="wide")
load_dotenv()  # lee .env si existe

DATA_PATH = os.path.join("data", "resources_sample.csv")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
DEFAULT_CITY = "San Diego, US"
DEFAULT_COORDS = (32.7157, -117.1611)  # centro SD

# -----------------------------
# Utils
# -----------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def load_resources():
    if not os.path.exists(DATA_PATH):
        return pd.DataFrame(columns=["name","type","address","lat","lon","hours","phone","notes"])
    df = pd.read_csv(DATA_PATH)
    # sanea tipos
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    return df.dropna(subset=["lat","lon"])

# -----------------------------
# DB simple para Salud (SQLite)
# -----------------------------
DB_PATH = "openhealth.db"

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS persons(
            id TEXT PRIMARY KEY,
            alias TEXT,
            birth_year INTEGER,
            conditions TEXT,
            meds TEXT,
            allergies TEXT,
            critical_flags TEXT,
            notes TEXT,
            updated_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id TEXT,
            when_ TEXT,
            provider TEXT,
            summary TEXT,
            FOREIGN KEY(person_id) REFERENCES persons(id)
        )
    """)
    con.commit()
    con.close()

def upsert_person(p):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT id FROM persons WHERE id=?", (p["id"],))
    exists = cur.fetchone() is not None
    now = datetime.utcnow().isoformat()
    if exists:
        cur.execute("""
            UPDATE persons SET alias=?, birth_year=?, conditions=?, meds=?, allergies=?,
                   critical_flags=?, notes=?, updated_at=?
            WHERE id=?
        """, (p["alias"], p["birth_year"], p["conditions"], p["meds"], p["allergies"],
              p["critical_flags"], p["notes"], now, p["id"]))
    else:
        cur.execute("""
            INSERT INTO persons(id, alias, birth_year, conditions, meds, allergies, critical_flags, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (p["id"], p["alias"], p["birth_year"], p["conditions"], p["meds"], p["allergies"],
              p["critical_flags"], p["notes"], now))
    con.commit()
    con.close()

def get_person(person_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT id, alias, birth_year, conditions, meds, allergies, critical_flags, notes, updated_at FROM persons WHERE id=?", (person_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    keys = ["id","alias","birth_year","conditions","meds","allergies","critical_flags","notes","updated_at"]
    return dict(zip(keys, row))

def add_visit(person_id, when_, provider, summary):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT INTO visits(person_id, when_, provider, summary) VALUES(?,?,?,?)",
                (person_id, when_, provider, summary))
    con.commit()
    con.close()

def get_visits(person_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT when_, provider, summary FROM visits WHERE person_id=? ORDER BY when_ DESC", (person_id,))
    rows = cur.fetchall()
    con.close()
    return [{"when": r[0], "provider": r[1], "summary": r[2]} for r in rows]

def make_qr_png(data_str):
    qr = qrcode.QRCode(version=2, box_size=6, border=2)
    qr.add_data(data_str)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf




# -----------------------------
# UI: Header con tabs
# -----------------------------
st.title("OpenHealth SD - MVP")

tabs = st.tabs(["Mapa", "Salud QR", "Recursos", "News & Clima"])

# -----------------------------
# Tab 1: Mapa
# -----------------------------
with tabs[0]:
    st.subheader("Mapa de recursos")
    df = load_resources()
    if df.empty:
        st.info("Sin datos. Reemplaza data/resources_sample.csv con datasets del hackathon.")
    colf1, colf2, colf3 = st.columns([2,1,1])
    with colf1:
        tipos = sorted(df["type"].dropna().unique().tolist())
        sel_tipos = st.multiselect("Filtrar por tipo", tipos, default=tipos)
    with colf2:
        search_txt = st.text_input("Buscar por nombre o direccion", "")
    with colf3:
        user_lat = st.number_input(
            "Tu lat",
            value=float(DEFAULT_COORDS[0]),
            format="%.6f",
            key="map_lat",
        )
        user_lon = st.number_input(
            "Tu lon",
            value=float(DEFAULT_COORDS[1]),
            format="%.6f",
            key="map_lon",
        )

    radius_km = st.slider(
        "Radio km (mostrar cercanos primero)",
        1,
        30,
        10,
        1,
        key="map_radius",
    )


    filtered = df[df["type"].isin(sel_tipos)].copy()
    if search_txt.strip():
        s = search_txt.lower()
        filtered = filtered[filtered.apply(
            lambda r: s in str(r["name"]).lower() or s in str(r["address"]).lower(), axis=1
        )]
    # Distancia y orden
    filtered["dist_km"] = filtered.apply(
        lambda r: haversine_km(user_lat, user_lon, r["lat"], r["lon"]),
        axis=1,
    )
    filtered = filtered.sort_values("dist_km")
    filtered = filtered[ filtered["dist_km"] <= radius_km ]


    # Capa pydeck
    tooltip = {
        "html": "<b>{name}</b><br/>{type}<br/>{address}<br/>Tel: {phone}<br/>Horario: {hours}<br/>{notes}",
        "style": {"backgroundColor": "white", "color": "black"}
    }
    # Colores por tipo
    # Colores por tipo
    color_map = {
        "Shelter":   [200, 0,   0],
        "Food":      [0,   120, 0],
        "Medical":   [0,   0,   200],
        "Hygiene":   [120, 120, 0],
        "Community": [120, 0,   120],
    }

# Para cada fila, asigna el color segun el tipo. Si no existe en el mapa, usa gris [80,80,80]
    filtered["color"] = filtered["type"].apply(
        lambda t: color_map.get(str(t), [80, 80, 80])   
    )


    layer = pdk.Layer(
        "ScatterplotLayer",
        data=filtered,
        get_position="[lon, lat]",
        get_radius=80,
        get_fill_color="color",
        pickable=True
    )
    view_state = pdk.ViewState(latitude=user_lat, longitude=user_lon, zoom=12, pitch=0)
    st.pydeck_chart(pdk.Deck(layers=[layer], initial_view_state=view_state, tooltip=tooltip, map_style="mapbox://styles/mapbox/light-v9"))

    st.caption("Tip: reemplaza el CSV por el dataset oficial (shelters, food banks, clinicas, etc.).")
    st.dataframe(filtered[["name","type","address","dist_km","hours","phone","notes"]].reset_index(drop=True))

# -----------------------------
# Tab 2: Salud QR
# -----------------------------
with tabs[1]:
    st.subheader("Expediente clinico basico + QR")
    init_db()

    mode = st.radio("Accion", ["Registrar/Actualizar", "Consultar por ID"], horizontal=True)

    if mode == "Registrar/Actualizar":
        with st.form("f_person"):
            c1, c2 = st.columns(2)
            with c1:
                alias = st.text_input("Alias (no poner nombre real)", "")
                birth_year = st.number_input("Ano de nacimiento (opcional)", min_value=1900, max_value=2100, value=1985)
                conditions = st.text_area("Condiciones (diabetes, HTA, etc.)")
                meds = st.text_area("Medicamentos")
            with c2:
                allergies = st.text_area("Alergias")
                critical_flags = st.text_area("Flags criticos (ej. anticoagulante, epilepsia, VIH, etc.)")
                notes = st.text_area("Notas para personal medico")
            submit = st.form_submit_button("Guardar")

        if submit:
            person_id = str(uuid.uuid4())[:8]  # ID corto
            p = {
                "id": person_id,
                "alias": alias.strip() or "NA",
                "birth_year": int(birth_year) if birth_year else None,
                "conditions": conditions.strip(),
                "meds": meds.strip(),
                "allergies": allergies.strip(),
                "critical_flags": critical_flags.strip(),
                "notes": notes.strip()
            }
            upsert_person(p)
            st.success(f"Guardado. ID asignado: {person_id}")

            # QR + PDF
            qr_png = make_qr_png(person_id)
            st.image(qr_png, caption=f"QR ID {person_id}", width=160)
            visits = []
            pdf_buf = pdf_from_person(get_person(person_id), visits)
            st.download_button("Descargar PDF", data=pdf_buf, file_name=f"openhealth_{person_id}.pdf", mime="application/pdf")

            st.info("Puedes imprimir el PDF y pegar el QR en una tarjeta. El QR solo contiene el ID del expediente.")

    else:
        qid = st.text_input("ID (desde QR o tarjeta)", "")
        if st.button("Cargar"):
            person = get_person(qid.strip())
            if not person:
                st.warning("No encontrado")
            else:
                st.success(f"Expediente {qid}")
                st.json(person)
                visits = get_visits(qid)
                st.write("Visitas:")
                if visits:
                    st.table(pd.DataFrame(visits))
                else:
                    st.caption("Sin visitas registradas")

                with st.form("f_visit"):
                    when_ = st.text_input("Fecha y hora (ISO, ej. 2025-10-26T10:30)", value=datetime.utcnow().isoformat(timespec="minutes"))
                    provider = st.text_input("Proveedor (clinica, paramedico, etc.)", "")
                    summary = st.text_area("Resumen clinico")
                    submit_v = st.form_submit_button("Agregar visita")
                if submit_v:
                    add_visit(qid, when_, provider, summary)
                    st.success("Visita agregada")
                    st.experimental_rerun()

                # Exportar PDF actualizado
                if st.button("Exportar PDF"):
                    visits = get_visits(qid)
                    pdf_buf = pdf_from_person(person, visits)
                    st.download_button("Descargar PDF", data=pdf_buf, file_name=f"openhealth_{qid}.pdf", mime="application/pdf")

# -----------------------------
# Tab 3: Recursos
# -----------------------------
with tabs[2]:
    st.subheader("Recursos y grupos de ayuda")
    df_all = load_resources()
    st.write("Directorio rapido (filtra por tipo o busca):")
    c1, c2 = st.columns([1,2])
    with c1:
        sel = st.multiselect("Tipo", sorted(df_all["type"].unique().tolist()), default=None)
    with c2:
        q = st.text_input("Buscar texto", "")
    dff = df_all.copy()
    if sel:
        dff = dff[dff["type"].isin(sel)]
    if q.strip():
        s = q.lower()
        dff = dff[dff.apply(lambda r: s in str(r["name"]).lower() or s in str(r["address"]).lower() or s in str(r["notes"]).lower(), axis=1)]
    st.dataframe(dff[["name","type","address","hours","phone","notes"]].reset_index(drop=True), use_container_width=True)

    st.caption("Para usar datasets oficiales, reemplaza data/resources_sample.csv por el CSV del hackathon (shelters, food, medical, hygiene, community).")

# -----------------------------
# Tab 4: News & Clima
# -----------------------------
with tabs[3]:
    st.subheader("News & clima para recomendaciones")
    city = st.text_input("Ciudad para clima", value=DEFAULT_CITY, key="wx_city")

    lat_user = st.number_input(
        "Tu lat",
        value=float(DEFAULT_COORDS[0]),
        format="%.6f",
        key="wx_lat",
    )
    lon_user = st.number_input(
        "Tu lon",
        value=float(DEFAULT_COORDS[1]),
        format="%.6f",
        key="wx_lon",
    )

    weather_box = st.empty()

    def get_weather(city_name):
        if not OPENWEATHER_API_KEY:
            return None, "Sin API key. Pon OPENWEATHER_API_KEY en .env para clima real."
        try:
            url = f"https://api.openweathermap.org/data/2.5/weather?q={city_name}&appid={OPENWEATHER_API_KEY}&units=metric"
            r = requests.get(url, timeout=8)
            if r.status_code != 200:
                return None, f"Error weather API: {r.text}"
            return r.json(), None
        except Exception as e:
            return None, str(e)

    wx, err = get_weather(city)
    if err:
        st.warning(err)
    else:
        temp_c = wx["main"]["temp"]
        desc = wx["weather"][0]["description"]
        st.metric("Temperatura", f"{temp_c:.1f} C")
        st.write("Condicion:", desc)

        cold = temp_c <= 8.0 or "cold" in desc.lower()
        heat = temp_c >= 35.0
        df_all = load_resources()
        shelters = df_all[df_all["type"]=="Shelter"].copy()
        if not shelters.empty:
            shelters["dist_km"] = shelters.apply(lambda r: haversine_km(lat_user, lon_user, r["lat"], r["lon"]), axis=1)
            shelters = shelters.sort_values("dist_km").head(5)

        if cold:
            st.error("Hace mucho frio. Recomendacion: busca un shelter cercano.")
            if not shelters.empty:
                st.table(shelters[["name","address","dist_km","hours","phone"]])
        elif heat:
            st.warning("Alto calor. Recomendacion: puntos de agua o centros con aire acondicionado.")
            water = df_all[df_all["type"].isin(["Hygiene","Community"])].copy()
            if not water.empty:
                water["dist_km"] = water.apply(lambda r: haversine_km(lat_user, lon_user, r["lat"], r["lon"]), axis=1)
                st.table(water.sort_values("dist_km").head(5)[["name","type","address","dist_km","hours","phone"]])
        else:
            st.info("Clima moderado. Usa el mapa para ver recursos cercanos.")

    st.caption("Para noticias locales, puedes integrar un RSS o API y mapear keywords como cold, storm, heat para activar alertas.")
