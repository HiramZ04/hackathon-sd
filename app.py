import os
import math
import uuid
import sqlite3
from datetime import datetime
from io import BytesIO

import pandas as pd
import streamlit as st
import pydeck as pdk
from fpdf import FPDF
import qrcode
from PIL import Image

# -------------------------------------------------
# CONFIG
# -------------------------------------------------
st.set_page_config(page_title="OpenHealth SD - MVP", layout="wide")

DATA_PATH = os.path.join("data", "resources_sample.csv")

# Centro aproximado (San Diego downtown)
DEFAULT_COORDS = (32.7157, -117.1611)

# DB local SQLite
DB_PATH = "openhealth.db"

# URL base que meteremos en el QR
# Para demo local usamos localhost. Si deployas en Streamlit Cloud,
# cambia esto a la URL publica de tu app (ej. https://tuapp.streamlit.app)
BASE_URL = "http://localhost:8501"


# -------------------------------------------------
# UTILS: distancia y carga de recursos
# -------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    """
    Distancia entre (lat1,lon1) y (lat2,lon2) en km usando haversine.
    """
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def load_resources():
    """
    Carga el CSV con ubicaciones de shelters, comida, etc.
    Si no existe o esta vacio, devuelve DataFrame vacio con columnas esperadas.
    """
    if not os.path.exists(DATA_PATH):
        return pd.DataFrame(
            columns=["name", "type", "address", "lat", "lon", "hours", "phone", "notes"]
        )

    try:
        df = pd.read_csv(DATA_PATH)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(
            columns=["name", "type", "address", "lat", "lon", "hours", "phone", "notes"]
        )

    # Asegurar numeric
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"])
    return df


# -------------------------------------------------
# DB LAYER
# -------------------------------------------------
def init_db():
    """
    Crea tablas si no existen:
    - persons: perfil medico basico
    - visits: visitas clinicas historicas
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
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
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS visits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id TEXT,
            when_ TEXT,
            provider TEXT,
            summary TEXT,
            FOREIGN KEY(person_id) REFERENCES persons(id)
        )
        """
    )
    con.commit()
    con.close()


def upsert_person(p):
    """
    Inserta o actualiza una persona (perfil medico).
    p = {
      "id": ...,
      "alias": ...,
      "birth_year": ...,
      "conditions": ...,
      "meds": ...,
      "allergies": ...,
      "critical_flags": ...,
      "notes": ...
    }
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT id FROM persons WHERE id=?", (p["id"],))
    exists = cur.fetchone() is not None
    now = datetime.utcnow().isoformat()

    if exists:
        cur.execute(
            """
            UPDATE persons
            SET alias=?, birth_year=?, conditions=?, meds=?, allergies=?,
                critical_flags=?, notes=?, updated_at=?
            WHERE id=?
            """,
            (
                p["alias"],
                p["birth_year"],
                p["conditions"],
                p["meds"],
                p["allergies"],
                p["critical_flags"],
                p["notes"],
                now,
                p["id"],
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO persons(id, alias, birth_year, conditions, meds,
                                allergies, critical_flags, notes, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                p["id"],
                p["alias"],
                p["birth_year"],
                p["conditions"],
                p["meds"],
                p["allergies"],
                p["critical_flags"],
                p["notes"],
                now,
            ),
        )

    con.commit()
    con.close()


def get_person(person_id):
    """
    Devuelve un dict con los datos de la persona, o None si no existe.
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        SELECT id, alias, birth_year, conditions, meds, allergies,
               critical_flags, notes, updated_at
        FROM persons
        WHERE id=?
        """,
        (person_id,),
    )
    row = cur.fetchone()
    con.close()

    if not row:
        return None

    keys = [
        "id",
        "alias",
        "birth_year",
        "conditions",
        "meds",
        "allergies",
        "critical_flags",
        "notes",
        "updated_at",
    ]
    return dict(zip(keys, row))


def add_visit(person_id, when_, provider, summary):
    """
    Agrega visita clinica al historial.
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO visits(person_id, when_, provider, summary) VALUES(?,?,?,?)",
        (person_id, when_, provider, summary),
    )
    con.commit()
    con.close()


def get_visits(person_id):
    """
    Devuelve lista de visitas [{when, provider, summary}, ...]
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        SELECT when_, provider, summary
        FROM visits
        WHERE person_id=?
        ORDER BY when_ DESC
        """,
        (person_id,),
    )
    rows = cur.fetchall()
    con.close()
    return [{"when": r[0], "provider": r[1], "summary": r[2]} for r in rows]


# -------------------------------------------------
# QR + PDF
# -------------------------------------------------
def make_qr_png(data_str):
    """
    Genera un PNG en memoria con el QR que contiene data_str.
    """
    qr = qrcode.QRCode(version=2, box_size=6, border=2)
    qr.add_data(data_str)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def pdf_from_person(person, visits):
    """
    Genera un PDF (BytesIO) con la info medica + historial + QR que apunta
    a BASE_URL?id=<person_id>.
    """
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, "OpenHealth SD - Expediente", ln=True)

    pdf.set_font("Arial", "", 11)

    def safe_txt(x):
        if x is None:
            return "-"
        s = str(x).strip()
        return s if s != "" else "-"

    def write_block(label, value, color=None):
        if color:
            pdf.set_text_color(*color)
        else:
            pdf.set_text_color(0, 0, 0)
        pdf.multi_cell(0, 7, f"{label}: {safe_txt(value)}")
        pdf.ln(1)

    # Datos basicos
    write_block("ID", person.get("id"))
    write_block("Alias", person.get("alias"))
    write_block("Ano nac", person.get("birth_year"))
    write_block("Condiciones", person.get("conditions"))
    write_block("Medicamentos", person.get("meds"))
    write_block("Alergias", person.get("allergies"))
    write_block("Flags criticos", person.get("critical_flags"), color=(200, 0, 0))
    write_block("Notas", person.get("notes"))

    pdf.ln(2)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Historial de visitas", ln=True)
    pdf.set_font("Arial", "", 11)

    if not visits:
        pdf.multi_cell(0, 7, "Sin visitas registradas")
        pdf.ln(1)
    else:
        for v in visits[:20]:
            block = f"- {v['when']} | {v['provider']}: {v['summary']}"
            pdf.multi_cell(0, 6, block)
            pdf.ln(1)

    # QR en el PDF
    pdf.ln(4)
    pdf.set_font("Arial", "I", 10)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 8, "QR acceso rapido", ln=True)

    qr_data = f"{BASE_URL}?id={person['id']}"
    qr_buf = make_qr_png(qr_data)
    tmp = BytesIO(qr_buf.read())
    tmp.seek(0)
    qr_img = Image.open(tmp)

    img_path = f"qr_{person['id']}.png"
    qr_img.save(img_path)
    pdf.image(img_path, x=10, y=pdf.get_y(), w=30)
    try:
        os.remove(img_path)
    except:
        pass

    output = BytesIO()
    pdf.output(output)
    output.seek(0)
    return output


# -------------------------------------------------
# INICIALIZAR DB
# -------------------------------------------------
init_db()

# leer query param ?id=xxxx del URL (para QR scan / deep link)
qp = st.query_params  # Streamlit moderno
incoming_id = qp.get("id")
if isinstance(incoming_id, list):
    incoming_id = incoming_id[0]


# -------------------------------------------------
# HEADER / NAV
# -------------------------------------------------
st.title("OpenHealth SD - MVP")
tabs = st.tabs(["Mapa", "Salud QR", "Recursos", "News & Clima"])


# -------------------------------------------------
# TAB 1: Mapa
# -------------------------------------------------
with tabs[0]:
    st.subheader("Mapa de recursos")

    df_map = load_resources()

    if df_map.empty:
        st.warning("No hay datos en data/resources_sample.csv todavia. Se mostrara demo.")
        # demo minimo para que no truene el mapa
        demo_lat, demo_lon = DEFAULT_COORDS
        df_map = pd.DataFrame(
            [
                {
                    "name": "Demo Shelter",
                    "type": "Shelter",
                    "address": "123 Demo St",
                    "lat": demo_lat,
                    "lon": demo_lon,
                    "hours": "24 horas",
                    "phone": "619-000-0000",
                    "notes": "Ejemplo demo",
                }
            ]
        )

    # --- filtros básicos ---
    tipos = sorted(df_map["type"].dropna().unique().tolist())
    sel_tipos = st.multiselect(
        "Filtrar por tipo",
        options=tipos,
        default=tipos,
    )

    col_a, col_b, col_c = st.columns([1, 1, 2])
    with col_a:
        max_km = st.slider(
            "Radio km (mostrar cercanos primero)",
            min_value=1,
            max_value=30,
            value=8,
        )
    with col_b:
        user_lat = st.number_input(
            "Tu lat",
            value=float(DEFAULT_COORDS[0]),
            format="%.6f",
        )
        user_lon = st.number_input(
            "Tu lon",
            value=float(DEFAULT_COORDS[1]),
            format="%.6f",
        )
    with col_c:
        search_txt = st.text_input(
            "Buscar por nombre/direccion (opcional)",
            value="",
        )

    # --- aplicar filtros ---
    work_df = df_map.copy()

    # por tipo
    if sel_tipos:
        work_df = work_df[work_df["type"].isin(sel_tipos)]

    # texto
    if search_txt.strip():
        low = search_txt.strip().lower()
        work_df = work_df[
            work_df["name"].str.lower().str.contains(low)
            | work_df["address"].str.lower().str.contains(low)
        ]

    # distancia
    work_df["dist_km"] = work_df.apply(
        lambda r: haversine_km(user_lat, user_lon, r["lat"], r["lon"]), axis=1
    )
    work_df = work_df.sort_values("dist_km")
    work_df = work_df[work_df["dist_km"] <= max_km]

    # mapa pydeck
    color_map = {
        "Shelter": [0, 92, 230],
        "Food": [0, 160, 60],
        "Medical": [200, 0, 0],
        "Hygiene": [200, 160, 0],
        "Community": [120, 0, 120],
    }
    work_df["color"] = work_df["type"].apply(
        lambda t: color_map.get(str(t), [200, 0, 200])
    )

    tooltip = {
        "html": "<b>{name}</b><br/>{type}<br/>{address}<br/>Tel: {phone}<br/>Horario: {hours}<br/>{notes}",
        "style": {"backgroundColor": "white", "color": "black"},
    }

    view_state = pdk.ViewState(
        latitude=user_lat,
        longitude=user_lon,
        zoom=12,
        pitch=0,
    )

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=work_df,
        get_position="[lon, lat]",
        get_radius=80,
        get_fill_color="color",
        pickable=True,
    )

    deck_obj = pdk.Deck(
        layers=[layer],
        initial_view_state=view_state,
        tooltip=tooltip,
        map_style="mapbox://styles/mapbox/dark-v10",
    )

    st.pydeck_chart(deck_obj, use_container_width=True, height=400)

    # tabla
    st.caption(
        "Tip: reemplaza el CSV por el dataset oficial de shelters, food banks, clinicas, etc."
    )
    st.dataframe(
        work_df[
            ["name", "type", "address", "dist_km", "hours", "phone", "notes"]
        ].reset_index(drop=True),
        use_container_width=True,
    )


# -------------------------------------------------
# TAB 2: Salud QR
# -------------------------------------------------
with tabs[1]:
    st.subheader("Salud QR")

    mode = st.radio(
        "Accion",
        ["Registrar / Actualizar", "Consultar por ID"],
        horizontal=True,
        key="qr_mode",
    )

    # ---------------------------
    # REGISTRAR / ACTUALIZAR
    # ---------------------------
    if mode == "Registrar / Actualizar":
        with st.form("f_person"):
            c1, c2 = st.columns(2)
            with c1:
                alias = st.text_input("Alias (no nombre legal)", "")
                birth_year = st.number_input(
                    "Ano nacimiento (opcional)",
                    min_value=1900,
                    max_value=2100,
                    value=1985,
                )
                conditions = st.text_area(
                    "Condiciones (ej. diabetes, HTA, asma, etc.)"
                )
                meds = st.text_area("Medicamentos")
            with c2:
                allergies = st.text_area("Alergias")
                critical_flags = st.text_area(
                    "Flags criticos (VIH+, epilepsia, anticoagulante, etc.)"
                )
                notes = st.text_area("Notas para personal medico")

            submit_new = st.form_submit_button("Guardar y generar QR")

        if submit_new:
            person_id = str(uuid.uuid4())[:8]  # ID corto tipo ab12cd34

            p = {
                "id": person_id,
                "alias": alias.strip() or "NA",
                "birth_year": int(birth_year) if birth_year else None,
                "conditions": conditions.strip(),
                "meds": meds.strip(),
                "allergies": allergies.strip(),
                "critical_flags": critical_flags.strip(),
                "notes": notes.strip(),
            }
            upsert_person(p)

            st.success(f"Guardado. ID asignado: {person_id}")

            # QR con link directo usando ?id=<ID>
            qr_url = f"{BASE_URL}?id={person_id}"
            qr_png_buf = make_qr_png(qr_url)

            left, right = st.columns([1, 3])
            with left:
                st.image(qr_png_buf, caption=f"QR ID {person_id}", width=160)
            with right:
                st.write(
                    "Escanear este QR abre OpenHealth con tu ID. "
                    "Personal medico puede ver alergias / condiciones rapido "
                    "sin pedir tu nombre legal."
                )
                st.write(
                    "Tambien puedes bajar tu hoja medica (PDF) e imprimirla."
                )

                visits_list = []
                pdf_buf = pdf_from_person(get_person(person_id), visits_list)
                st.download_button(
                    "Descargar PDF medico",
                    data=pdf_buf,
                    file_name=f"openhealth_{person_id}.pdf",
                    mime="application/pdf",
                )

            st.write("Vista rapida de los datos guardados:")
            st.json(get_person(person_id))

    # ---------------------------
    # CONSULTAR POR ID
    # ---------------------------
    else:
        st.write(
            "Pega el ID (o escanea un QR que abre esta pagina con ?id=TU_ID y se rellena solo)."
        )

        with st.form("f_lookup"):
            qid_default = incoming_id if incoming_id else ""
            qid = st.text_input(
                "ID paciente",
                value=qid_default,
                key="lookup_text",
            )
            load_btn = st.form_submit_button("Cargar expediente")

        if load_btn and qid.strip():
            person = get_person(qid.strip())
            if not person:
                st.warning("No encontrado")
            else:
                st.success(f"Expediente {qid.strip()}")
                st.json(person)

                # visitas existentes
                visits_now = get_visits(qid.strip())
                st.subheader("Visitas registradas")
                if visits_now:
                    st.table(pd.DataFrame(visits_now))
                else:
                    st.caption("Sin visitas registradas")

                # form para agregar visita
                st.subheader("Agregar visita clinica")
                with st.form("f_visit"):
                    when_ = st.text_input(
                        "Fecha y hora (ISO)",
                        value=datetime.utcnow().isoformat(timespec="minutes"),
                    )
                    provider = st.text_input(
                        "Proveedor (clinica, paramedico, etc.)", ""
                    )
                    summary = st.text_area("Resumen clinico")
                    submit_v = st.form_submit_button("Agregar visita")

                if submit_v:
                    add_visit(qid.strip(), when_, provider, summary)
                    st.success(
                        "Visita agregada. Vuelve a 'Cargar expediente' para refrescar la tabla."
                    )

                # exportar PDF actualizado
                st.subheader("Exportar PDF actualizado")
                visits_now = get_visits(qid.strip())
                pdf_buf = pdf_from_person(person, visits_now)
                st.download_button(
                    "Descargar PDF medico (actualizado)",
                    data=pdf_buf,
                    file_name=f"openhealth_{qid.strip()}.pdf",
                    mime="application/pdf",
                )


# -------------------------------------------------
# TAB 3: Recursos
# -------------------------------------------------
with tabs[2]:
    st.subheader("Recursos comunitarios")

    df_all = load_resources()
    if df_all.empty:
        st.warning("No hay datos todavia en data/resources_sample.csv.")
    else:
        st.dataframe(
            df_all[
                ["name", "type", "address", "hours", "phone", "notes"]
            ].reset_index(drop=True),
            use_container_width=True,
        )

    st.caption(
        "Ejemplos de shelters, bancos de comida, clinicas, apoyo legal, higiene, etc."
    )


# -------------------------------------------------
# TAB 4: News & Clima
# -------------------------------------------------
with tabs[3]:
    st.subheader("Noticias y Clima")

    st.write(
        "⚠️ Clima frio esta noche. Busca un shelter cercano para dormir bajo techo."
    )
    st.write(
        "Recomendacion: mantente hidratado, utiliza cobijas secas y evita zonas aisladas."
    )
    st.write("Si necesitas atencion medica urgente marca 911.")
    st.write("Si necesitas apoyo no urgente, pide ayuda en el perfil 'Salud QR'.")
