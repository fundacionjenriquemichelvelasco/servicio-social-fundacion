import datetime
import io
import random
import re
import unicodedata
import zipfile
import requests
import streamlit as st
from supabase import create_client, Client

# -------------------------------------------------------------
# 1. CONEXIÓN SEGURA CON SUPABASE
# -------------------------------------------------------------
SUPABASE_URL = st.secrets.get("SUPABASE_URL")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("⚠️ Falta la configuración de SUPABASE_URL o SUPABASE_KEY en `.streamlit/secrets.toml`.")
    st.stop()

@st.cache_resource
def init_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

supabase = init_supabase()

def limpiar_texto(texto: str) -> str:
    """Elimina acentos y caracteres especiales para rutas de archivos y nombres."""
    nfkd_form = unicodedata.normalize('NFKD', texto)
    texto_sin_acentos = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    return re.sub(r'[^a-zA-Z0-9_\-]', '_', texto_sin_acentos)

def calcular_y_actualizar_horas(id_servicio: int, grupo_semestre: str):
    """Calcula automáticamente las horas acumuladas por asistencias y tareas sin duplicar."""
    try:
        res_cfg = supabase.table("configuracion_general").select("valor").eq("clave", "horas_por_asistencia").execute()
        val_asistencia = int(res_cfg.data[0]["valor"]) if res_cfg.data else 2

        res_asist = supabase.table("asistencias").select("id_asistencia", count="exact").eq("id_servicio", id_servicio).execute()
        cant_asistencias = res_asist.count if res_asist.count is not None else 0
        total_horas_asistencias = cant_asistencias * val_asistencia

        res_inv = supabase.table("investigaciones").select("titulo_entrega").eq("id_servicio", id_servicio).execute()
        
        # Filtramos entregas únicas por título para evitar sumar doble
        entregas_unicas = set(e["titulo_entrega"] for e in res_inv.data) if res_inv.data else set()
        
        total_horas_entregables = 0
        if entregas_unicas:
            res_cat = supabase.table("catalogo_entregables").select("nombre_entregable, horas_valor").eq("grupo_semestre", grupo_semestre).execute()
            mapa_horas = {item["nombre_entregable"]: item["horas_valor"] for item in res_cat.data} if res_cat.data else {}
            
            for titulo in entregas_unicas:
                total_horas_entregables += mapa_horas.get(titulo, 0)

        total_acumulado = total_horas_asistencias + total_horas_entregables
        supabase.table("servicio_social").update({"horas_acumuladas": total_acumulado}).eq("id_servicio", id_servicio).execute()

    except Exception as e:
        print(f"Error al recalcular horas: {e}")

# -------------------------------------------------------------
# CONFIGURACIÓN DE PÁGINA
# -------------------------------------------------------------
st.set_page_config(page_title="Gestión de Servicio Social", page_icon="🎓", layout="wide")

if "usuario_rol" not in st.session_state:
    st.session_state["usuario_rol"] = None
if "usuario_datos" not in st.session_state:
    st.session_state["usuario_datos"] = None

# =============================================================
# 2. PANTALLA ÚNICA DE AUTENTICACIÓN / REGISTRO
# =============================================================
if st.session_state["usuario_rol"] is None:
    st.title("🎓 Servicio Social - Portal de Acceso")
    
    tab_login, tab_registro = st.tabs(["Iniciar Sesión", "Registro de Nuevos Estudiantes"])

    with tab_login:
        st.subheader("Ingreso al Sistema")
        
        tipo_usuario = st.radio("Selecciona tu rol:", ["Estudiante (Matrícula)", "Administradora (Correo)"], horizontal=True)

        if tipo_usuario == "Estudiante (Matrícula)":
            usuario_input = st.text_input("Ingresa tu Matrícula")
            if st.button("Entrar como Estudiante"):
                if usuario_input.strip():
                    res_al = supabase.table("alumnos").select("*").eq("matricula", usuario_input.strip()).execute()
                    if res_al.data:
                        st.session_state["usuario_rol"] = "estudiante"
                        st.session_state["usuario_datos"] = res_al.data[0]
                        st.success(f"Bienvenido/a {res_al.data[0]['nombre']}")
                        st.rerun()
                    else:
                        st.error("Matrícula no encontrada. Si eres nuevo estudiante, regístrate en la pestaña de al lado.")

        else:
            st.info("Ingresa tus credenciales administrativas para iniciar sesión.")
            
            # --- NUEVO FORMULARIO DE INICIO DE SESIÓN CON CONTRASEÑA ---
            with st.form("admin_login_form"):
                email_admin = st.text_input("Correo electrónico de la Administradora")
                password_admin = st.text_input("Contraseña", type="password")
                submit_admin = st.form_submit_button("Iniciar Sesión")

            # --- BOTÓN TEMPORAL PARA REGISTRAR LA CUENTA OFICIAL ---
            st.markdown("---")
            if st.button("➕ Crear / Registrar esta cuenta de Administradora"):
                if email_admin and password_admin:
                    try:
                        res = supabase.auth.sign_up({
                            "email": email_admin.strip(),
                            "password": password_admin.strip()
                        })
                        st.success("¡Cuenta registrada con éxito en Supabase! Ahora ve a Supabase > Users y confirma el email si te lo solicita.")
                    except Exception as e:
                        st.error(f"Error al registrar: {e}")
                else:
                    st.warning("Escribe el correo y la contraseña arriba antes de presionar este botón.")

            if submit_admin:
                if not email_admin.strip() or not password_admin.strip():
                    st.warning("Por favor, ingresa tanto tu correo como tu contraseña.")
                else:
                    try:
                        # Autenticación directa en Supabase con correo y contraseña
                        res = supabase.auth.sign_in_with_password({
                            "email": email_admin.strip(),
                            "password": password_admin.strip()
                        })
                        if res.user:
                            st.session_state["usuario_rol"] = "admin"
                            st.session_state["usuario_datos"] = {"nombre": "Administradora", "email": email_admin.strip()}
                            st.success("¡Sesión iniciada correctamente!")
                            st.rerun()
                    except Exception as e:
                        st.error("Correo o contraseña incorrectos. Verifica tus credenciales en Supabase.")

    with tab_registro:
        st.subheader("Registro Inicial de Alumnos")
        
        grupos_lista = []
        try:
            res_grp = supabase.table("grupos").select("nombre_grupo").eq("estado_grupo", "En Proceso").order("nombre_grupo").execute()
            if res_grp.data:
                grupos_lista = [g["nombre_grupo"] for g in res_grp.data]
        except Exception:
            grupos_lista = []

        if not grupos_lista:
            st.warning("⚠️ No hay grupos activos registrados por la administración.")
        else:
            with st.form("form_registro", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    matricula = st.text_input("Matrícula / Código escolar*")
                    nombre = st.text_input("Nombre completo*")
                    contacto = st.text_input("Teléfono de contacto*")
                    correo = st.text_input("Correo electrónico*")
                with col2:
                    carrera = st.text_input("Carrera / Licenciatura*")
                    grupo_auto = st.selectbox("Selecciona tu Periodo / Grupo Escolar*", grupos_lista)
                    archivo_foda = st.file_uploader("Subir FODA (Solo PDF)*", type=["pdf"])

                enviado = st.form_submit_button("Completar Registro")

            if enviado:
                if not matricula or not nombre or not contacto or not correo or not carrera or archivo_foda is None:
                    st.error("Por favor completa todos los campos obligatorios (*).")
                else:
                    try:
                        mat_limpia = limpiar_texto(matricula.strip())
                        grp_limpio = limpiar_texto(grupo_auto)
                        
                        ruta_foda = f"expedientes/{grp_limpio}/{mat_limpia}/foda/{mat_limpia}_foda.pdf"
                        bytes_archivo = archivo_foda.getvalue()

                        supabase.storage.from_("fodas").upload(
                            path=ruta_foda,
                            file=bytes_archivo,
                            file_options={"content-type": "application/pdf", "upsert": "true"}
                        )

                        url_foda = supabase.storage.from_("fodas").get_public_url(ruta_foda)

                        res_alumno = supabase.table("alumnos").upsert({
                            "matricula": matricula.strip(),
                            "nombre": nombre.strip(),
                            "contacto": contacto.strip(),
                            "correo": correo.strip(),
                            "carrera": carrera.strip()
                        }, on_conflict="matricula").execute()

                        id_alumno = res_alumno.data[0]["id_alumno"]

                        supabase.table("servicio_social").insert({
                            "id_alumno": id_alumno,
                            "periodo": grupo_auto,
                            "anio": datetime.datetime.now().year,
                            "grupo_semestre": grupo_auto,
                            "url_foda": url_foda,
                            "horas_acumuladas": 0,
                            "estado": "En Proceso"
                        }).execute()

                        st.success("✅ Registro completado exitosamente. Ahora puedes iniciar sesión con tu matrícula.")
                    except Exception as e:
                        st.error(f"Error durante el registro: {e}")

# =============================================================
# 3. INTERFAZ DE ESTUDIANTES
# =============================================================
elif st.session_state["usuario_rol"] == "estudiante":
    alumno = st.session_state["usuario_datos"]
    st.sidebar.write(f"**Estudiante:** {alumno['nombre']}")
    st.sidebar.write(f"**Matrícula:** {alumno['matricula']}")
    if st.sidebar.button("Cerrar Sesión"):
        st.session_state["usuario_rol"] = None
        st.rerun()

    st.title("🎓 Portal del Estudiante")
    
    tab_e1, tab_e2 = st.tabs(["Registrar Asistencia con Foto", "Entregar Tareas / Investigaciones"])

    with tab_e1:
        st.subheader("Asistencia Diaria")
        
        with st.form("form_asist_estudiante", clear_on_submit=True):
            codigo_ingresado = st.text_input("Código de Asistencia del día (Proporcionado por la administración)")
            foto_evidencia = st.camera_input("Toma una foto/selfie en las instalaciones como evidencia")
            btn_asist = st.form_submit_button("Registrar Asistencia")

        if btn_asist:
            if not codigo_ingresado or foto_evidencia is None:
                st.error("Debes ingresar el código del día y tomar una foto de evidencia.")
            else:
                try:
                    res_ss = supabase.table("servicio_social").select("id_servicio, grupo_semestre").eq("id_alumno", alumno["id_alumno"]).order("id_servicio", desc=True).execute()
                    if not res_ss.data:
                        st.error("No estás inscrito en ningún grupo activo.")
                    else:
                        id_servicio = res_ss.data[0]["id_servicio"]
                        grupo_semestre = res_ss.data[0]["grupo_semestre"]
                        hoy_str = str(datetime.date.today())

                        res_cod = supabase.table("codigos_asistencia").select("codigo").eq("fecha", hoy_str).execute()
                        if not res_cod.data or res_cod.data[0]["codigo"] != codigo_ingresado.strip():
                            st.error("El código del día es incorrecto o no ha sido generado.")
                        else:
                            mat_limpia = limpiar_texto(alumno["matricula"])
                            grp_limpio = limpiar_texto(grupo_semestre)
                            
                            nombre_foto = f"{hoy_str}_{mat_limpia}_asistencia.jpg"
                            ruta_foto = f"expedientes/{grp_limpio}/{mat_limpia}/asistencias/{nombre_foto}"
                            bytes_foto = foto_evidencia.getvalue()

                            supabase.storage.from_("investigaciones").upload(
                                path=ruta_foto,
                                file=bytes_foto,
                                file_options={"content-type": "image/jpeg", "upsert": "true"}
                            )
                            url_foto = supabase.storage.from_("investigaciones").get_public_url(ruta_foto)

                            supabase.table("asistencias").insert({
                                "id_alumno": alumno["id_alumno"],
                                "id_servicio": id_servicio,
                                "fecha": hoy_str,
                                "url_foto": url_foto
                            }).execute()

                            calcular_y_actualizar_horas(id_servicio, grupo_semestre)
                            st.success("¡Asistencia y evidencia fotográfica guardadas correctamente!")
                except Exception as e:
                    if "duplicate key value" in str(e) or "23505" in str(e):
                        st.warning("Ya registraste tu asistencia el día de hoy.")
                    else:
                        st.error(f"Error al registrar asistencia: {e}")

    with tab_e2:
        st.subheader("Entregables e Investigaciones")
        try:
            res_ss = supabase.table("servicio_social").select("id_servicio, grupo_semestre").eq("id_alumno", alumno["id_alumno"]).order("id_servicio", desc=True).execute()
            if res_ss.data:
                id_servicio = res_ss.data[0]["id_servicio"]
                grupo_actual = res_ss.data[0]["grupo_semestre"]

                res_cat = supabase.table("catalogo_entregables").select("*").eq("grupo_semestre", grupo_actual).eq("activo", True).execute()
                if not res_cat.data:
                    st.info("No hay tareas asignadas para tu grupo por el momento.")
                else:
                    mapa_tareas = {item["nombre_entregable"]: item for item in res_cat.data}
                    tarea_sel = st.selectbox("Selecciona la Tarea a entregar:", list(mapa_tareas.keys()))
                    tarea_obj = mapa_tareas[tarea_sel]

                    st.write(f"**Horas asignadas:** {tarea_obj['horas_valor']} hrs")
                    st.write(f"**Instrucciones:** {tarea_obj.get('descripcion', 'Sin instrucciones.')}")
                    
                    # Verificación de Fecha Límite
                    fecha_limite_str = tarea_obj.get("fecha_limite")
                    es_a_tiempo = True
                    
                    if fecha_limite_str:
                        limite_dt = datetime.datetime.fromisoformat(fecha_limite_str.replace("Z", "+00:00"))
                        ahora_dt = datetime.datetime.now(datetime.timezone.utc)
                        st.warning(f"**Fecha límite de entrega:** {limite_dt.strftime('%d/%m/%Y a las %H:%M hrs')}")
                        if ahora_dt > limite_dt:
                            es_a_tiempo = False

                    if not es_a_tiempo:
                        st.error("El tiempo para entregar esta tarea ha expirado. Ya no se aceptan archivos.")
                    else:
                        archivo_tarea = st.file_uploader("Adjuntar archivo PDF", type=["pdf"])
                        if st.button("Enviar Tarea"):
                            if archivo_tarea is None:
                                st.error("Adjunta el archivo PDF correspondiente.")
                            else:
                                mat_limpia = limpiar_texto(alumno["matricula"])
                                grp_limpio = limpiar_texto(grupo_actual)
                                nom_tarea_limpia = limpiar_texto(tarea_obj['nombre_entregable'])

                                ruta_tarea = f"expedientes/{grp_limpio}/{mat_limpia}/entregables/{nom_tarea_limpia}.pdf"
                                supabase.storage.from_("investigaciones").upload(
                                    path=ruta_tarea,
                                    file=archivo_tarea.getvalue(),
                                    file_options={"content-type": "application/pdf", "upsert": "true"}
                                )
                                url_tarea = supabase.storage.from_("investigaciones").get_public_url(ruta_tarea)

                                supabase.table("investigaciones").delete().eq("id_servicio", id_servicio).eq("titulo_entrega", tarea_obj['nombre_entregable']).execute()

                                supabase.table("investigaciones").insert({
                                    "id_alumno": alumno["id_alumno"],
                                    "id_servicio": id_servicio,
                                    "titulo_entrega": tarea_obj['nombre_entregable'],
                                    "url_pdf": url_tarea
                                }).execute()

                                calcular_y_actualizar_horas(id_servicio, grupo_actual)
                                st.success("✅ Tarea enviada exitosamente.")
        except Exception as e:
            st.error(f"Error al cargar módulo de entregables: {e}")

# =============================================================
# 4. PANEL EXCLUSIVO DE LA ADMINISTRADORA
# =============================================================
elif st.session_state["usuario_rol"] == "admin":
    st.sidebar.title("Administradora")
    if st.sidebar.button("Cerrar Sesión"):
        st.session_state["usuario_rol"] = None
        st.rerun()

    st.title("Control de Servicio Social")

    res_g_all = supabase.table("grupos").select("nombre_grupo, estado_grupo").order("nombre_grupo").execute()
    grupos_list = [g["nombre_grupo"] for g in res_g_all.data] if res_g_all.data else []

    if grupos_list:
        grupo_activo = st.selectbox("Selecciona el Grupo de Trabajo activo:", grupos_list)
    else:
        grupo_activo = None
        st.warning("Crea un grupo de trabajo en la pestaña 'Configuración y Grupos'.")

    tab_a1, tab_a2, tab_a3 = st.tabs([
        "Alumnos y Expediente", 
        "Asistencias y Revisiones de Tareas", 
        "⚙️ Configuración y Grupos"
    ])

    # ---------------------------------------------------------
    # TAB ADMIN 1: ALUMNOS, EDICIÓN Y DESCARGA ZIP
    # ---------------------------------------------------------
    with tab_a1:
        if grupo_activo:
            st.subheader(f"Estudiantes Registrados - {grupo_activo}")
            res_ss = supabase.table("servicio_social").select(
                "id_servicio, id_alumno, periodo, grupo_semestre, url_foda, horas_acumuladas, estado, alumnos(id_alumno, matricula, nombre, contacto, correo, carrera)"
            ).eq("grupo_semestre", grupo_activo).execute()

            if res_ss.data:
                tabla_alumnos = []
                opciones_alumnos = {}

                for d in res_ss.data:
                    al = d.get("alumnos", {}) or {}
                    mat_al = al.get("matricula", "")
                    nombre_al = al.get("nombre", "")
                    
                    label = f"{mat_al} - {nombre_al}"
                    opciones_alumnos[label] = d

                    tabla_alumnos.append({
                        "Matrícula": mat_al,
                        "Nombre": nombre_al,
                        "Carrera": al.get("carrera"),
                        "Contacto": al.get("contacto"),
                        "FODA": d.get("url_foda"),
                        "Horas Totales": f"{d['horas_acumuladas']} hrs",
                        "Estado": d["estado"]
                    })

                st.dataframe(
                    tabla_alumnos,
                    column_config={
                        "FODA": st.column_config.LinkColumn("FODA PDF", display_text="📄 Ver PDF")
                    },
                    use_container_width=True
                )

                st.markdown("---")
                col_exp1, col_exp2 = st.columns(2)

                # --- EDICIÓN Y ELIMINACIÓN ---
                with col_exp1:
                    st.subheader("Editar o Eliminar Alumno")
                    alumno_sel_edit = st.selectbox("Selecciona Alumno para gestionar:", list(opciones_alumnos.keys()))
                    reg_edit = opciones_alumnos[alumno_sel_edit]
                    al_data_edit = reg_edit.get("alumnos", {}) or {}

                    with st.expander("Formulario de Edición"):
                        new_nom = st.text_input("Nombre Completo:", value=al_data_edit.get("nombre", ""))
                        new_mat = st.text_input("Matrícula:", value=al_data_edit.get("matricula", ""))
                        new_car = st.text_input("Carrera:", value=al_data_edit.get("carrera", ""))
                        
                        col_btn1, col_btn2 = st.columns(2)
                        with col_btn1:
                            if st.button("Guardar Cambios"):
                                supabase.table("alumnos").update({
                                    "nombre": new_nom,
                                    "matricula": new_mat,
                                    "carrera": new_car
                                }).eq("id_alumno", al_data_edit["id_alumno"]).execute()
                                st.success("Alumno actualizado.")
                                st.rerun()
                        
                        with col_btn2:
                            if st.button("Eliminar Registro Completo"):
                                supabase.table("servicio_social").delete().eq("id_servicio", reg_edit["id_servicio"]).execute()
                                supabase.table("alumnos").delete().eq("id_alumno", al_data_edit["id_alumno"]).execute()
                                st.success("Registro y alumno borrados.")
                                st.rerun()

                # --- DESCARGA MASIVA ZIP EVIDENCIAS ---
                with col_exp2:
                    st.subheader("Descargar Expedientes Completos (ZIP)")
                    st.write("Genera una carpeta comprimida con subcarpetas estructuradas para la entrega oficial.")

                    if st.button("Generar Archivo ZIP de Evidencias"):
                        with st.spinner("Descargando fotos y tareas para armar el paquete ZIP..."):
                            zip_buffer = io.BytesIO()

                            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                                for reg in res_ss.data:
                                    al_info = reg.get("alumnos", {}) or {}
                                    mat = limpiar_texto(al_info.get("matricula", "desconocido"))
                                    nom = limpiar_texto(al_info.get("nombre", "alumno"))
                                    id_serv = reg["id_servicio"]

                                    carpeta_estudiante = f"{mat}_{nom}"

                                    res_fotos = supabase.table("asistencias").select("fecha, url_foto").eq("id_servicio", id_serv).execute()
                                    if res_fotos.data:
                                        for idx, f in enumerate(res_fotos.data):
                                            url_f = f.get("url_foto")
                                            if url_f:
                                                try:
                                                    resp = requests.get(url_f)
                                                    if resp.status_code == 200:
                                                        fecha_f = f.get("fecha", f"foto_{idx}")
                                                        nom_foto = f"{fecha_f}_{mat}_asistencia.jpg"
                                                        ruta_en_zip = f"{carpeta_estudiante}/Asistencias/{nom_foto}"
                                                        zip_file.writestr(ruta_en_zip, resp.content)
                                                except Exception:
                                                    pass

                                    res_tareas = supabase.table("investigaciones").select("titulo_entrega, url_pdf").eq("id_servicio", id_serv).execute()
                                    if res_tareas.data:
                                        for t in res_tareas.data:
                                            url_t = t.get("url_pdf")
                                            if url_t:
                                                try:
                                                    resp = requests.get(url_t)
                                                    if resp.status_code == 200:
                                                        nom_t = limpiar_texto(t.get("titulo_entrega", "tarea"))
                                                        ruta_en_zip = f"{carpeta_estudiante}/Tareas/{nom_t}.pdf"
                                                        zip_file.writestr(ruta_en_zip, resp.content)
                                                except Exception:
                                                    pass

                            zip_buffer.seek(0)
                            st.download_button(
                                label="Descargar Evidencias ZIP",
                                data=zip_buffer,
                                file_name=f"Evidencias_{limpiar_texto(grupo_activo)}.zip",
                                mime="application/zip"
                            )

    # ---------------------------------------------------------
    # TAB ADMIN 2: ASISTENCIAS Y REVISIÓN DE TAREAS
    # ---------------------------------------------------------
    with tab_a2:
        if grupo_activo:
            st.subheader(f"Asistencias y Tareas - {grupo_activo}")
            
            sub_tab1, sub_tab2 = st.tabs(["Asistencias del Día", "Revisión de Entregables por Tarea"])

            with sub_tab1:
                hoy_str = str(datetime.date.today())
                res_cod = supabase.table("codigos_asistencia").select("codigo").eq("fecha", hoy_str).execute()
                
                col_cod1, col_cod2 = st.columns(2)
                with col_cod1:
                    if res_cod.data:
                        st.metric("Código del Día:", res_cod.data[0]["codigo"])
                    else:
                        if st.button("Generar Código de Asistencia para Hoy"):
                            nuevo_codigo = str(random.randint(1000, 9999))
                            supabase.table("codigos_asistencia").insert({"fecha": hoy_str, "codigo": nuevo_codigo}).execute()
                            st.rerun()

                st.markdown("---")
                st.write("### Evidencias Fotográficas de Hoy")

                res_asist_fotos = supabase.table("asistencias").select(
                    "fecha, url_foto, created_at, alumnos(matricula, nombre)"
                ).eq("fecha", hoy_str).order("created_at", desc=True).execute()

                if res_asist_fotos.data:
                    cols = st.columns(4)
                    for idx, item in enumerate(res_asist_fotos.data):
                        al_data = item.get("alumnos", {}) or {}
                        foto_url = item.get("url_foto")
                        with cols[idx % 4]:
                            st.caption(f"{item['fecha']} - {al_data.get('nombre', 'Sin nombre')}")
                            if foto_url:
                                st.image(foto_url, use_container_width=True)
                else:
                    st.info("No hay registros de asistencias capturados el día de hoy.")

            with sub_tab2:
                st.write("### Consultar Tareas Entregadas por Estudiantes")
                res_cat_t = supabase.table("catalogo_entregables").select("nombre_entregable").eq("grupo_semestre", grupo_activo).execute()
                
                if not res_cat_t.data:
                    st.info("No se han publicado tareas para este grupo.")
                else:
                    lista_tareas = [t["nombre_entregable"] for t in res_cat_t.data]
                    tarea_filtro = st.selectbox("Selecciona la tarea a revisar:", lista_tareas)

                    res_entregas = supabase.table("investigaciones").select(
                        "created_at, url_pdf, alumnos(matricula, nombre)"
                    ).eq("titulo_entrega", tarea_filtro).execute()

                    if res_entregas.data:
                        tabla_rev = []
                        for ent in res_entregas.data:
                            al_info = ent.get("alumnos", {}) or {}
                            tabla_rev.append({
                                "Matrícula": al_info.get("matricula"),
                                "Alumno": al_info.get("nombre"),
                                "Fecha Entrega": ent.get("created_at", "")[:10],
                                "PDF": ent.get("url_pdf")
                            })

                        st.dataframe(
                            tabla_rev,
                            column_config={
                                "PDF": st.column_config.LinkColumn("Documento PDF", display_text="📄 Ver / Descargar PDF")
                            },
                            use_container_width=True
                        )
                    else:
                        st.warning("Ningún alumno ha subido respuesta para esta tarea aún.")

    # ---------------------------------------------------------
    # TAB ADMIN 3: CONFIGURACIÓN Y TAREAS
    # ---------------------------------------------------------
    with tab_a3:
        st.subheader("⚙️ Gestión de Grupos y Asignación de Tareas")
        
        col_g1, col_g2 = st.columns(2)
        with col_g1:
            st.write("### Crear Grupo Escolar")
            nuevo_grp = st.text_input("Nombre del Nuevo Grupo (Ej. Semestre 2026-2)")
            if st.button("Crear Grupo"):
                if nuevo_grp.strip():
                    supabase.table("grupos").insert({"nombre_grupo": nuevo_grp.strip(), "estado_grupo": "En Proceso"}).execute()
                    st.success("Grupo creado.")
                    st.rerun()

            st.markdown("---")
            st.write("### Concluir Semestre Actual")
            st.write("Cambia el estado de todos los alumnos de este grupo a **Concluido** para cerrar el periodo escolar.")
            
            if grupo_activo and st.button("Marcar Grupo Completo como Concluido"):
                supabase.table("servicio_social").update({"estado": "Concluido"}).eq("grupo_semestre", grupo_activo).execute()
                supabase.table("grupos").update({"estado_grupo": "Concluido"}).eq("nombre_grupo", grupo_activo).execute()
                st.success(f"El grupo '{grupo_activo}' ha sido marcado como Concluido.")
                st.rerun()

        with col_g2:
            st.write("### Nueva Tarea / Entregable con Límite")
            if grupo_activo:
                nom_tarea = st.text_input("Nombre de la Tarea")
                hrs_tarea = st.number_input("Horas que Otorga", min_value=1, value=10)
                desc_tarea = st.text_area("Instrucciones de la tarea")
                
                c_f, c_h = st.columns(2)
                with c_f:
                    f_limite = st.date_input("Fecha límite", value=datetime.date.today() + datetime.timedelta(days=7))
                with c_h:
                    h_limite = st.time_input("Hora límite", value=datetime.time(23, 59))
                
                if st.button("Asignar Tarea al Grupo"):
                    if nom_tarea.strip():
                        dt_combinado = datetime.datetime.combine(f_limite, h_limite)
                        iso_limite = dt_combinado.isoformat()
                        
                        supabase.table("catalogo_entregables").insert({
                            "grupo_semestre": grupo_activo,
                            "nombre_entregable": nom_tarea.strip(),
                            "horas_valor": int(hrs_tarea),
                            "descripcion": desc_tarea.strip(),
                            "fecha_limite": iso_limite,
                            "activo": True
                        }).execute()
                        st.success(f"Tarea '{nom_tarea}' creada para el grupo {grupo_activo}.")
                        st.rerun()
                    else:
                        st.error("Ingresa un nombre válido para la tarea.")
            else:
                st.info("Selecciona o crea un grupo primero para asignar tareas.")
