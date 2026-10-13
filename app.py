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

def calcular_horas_presenciales(check_in_dt, check_out_dt):
    """
    Calcula horas presenciales con redondeo por minutos y multiplicador x2.
    - Minutos 0-19: Redondea hacia abajo.
    - Minutos 20-39: Horas exactas acumuladas.
    - Minutos 40-59: Redondea hacia arriba.
    - Si pasan más de 8 horas sin check-out: Cancela (devuelve 0).
    """
    diferencia = check_out_dt - check_in_dt
    total_segundos = diferencia.total_seconds()
    
    # Regla: Si pasaron más de 8 horas (28,800 segundos), se cancela la asistencia
    if total_segundos > 28800 or total_segundos < 0:
        return 0, True  # (0 horas, cancelado=True)

    horas_enteras = int(total_segundos // 3600)
    minutos_restantes = int((total_segundos % 3600) // 60)

    if minutos_restantes < 20:
        horas_redondeadas = horas_enteras
    elif minutos_restantes >= 40:
        horas_redondeadas = horas_enteras + 1
    else:
        horas_redondeadas = horas_enteras

    # Multiplicador x2
    horas_finales = horas_redondeadas * 2
    return horas_finales, False

def calcular_y_actualizar_horas(id_servicio: int, grupo_semestre: str):
    """Suma las horas presenciales de asistencias cerradas + horas de tareas calculadas."""
    try:
        # 1. Sumar horas de asistencias válidas
        res_asist = supabase.table("asistencias").select("horas_otorgadas, estado").eq("id_servicio", id_servicio).eq("estado", "COMPLETADA").execute()
        total_asistencias = sum(a.get("horas_otorgadas", 0) for a in res_asist.data) if res_asist.data else 0

        # 2. Sumar horas de tareas
        res_inv = supabase.table("investigaciones").select("id_entregable, horas_ganadas").eq("id_servicio", id_servicio).execute()
        total_tareas = sum(i.get("horas_ganadas", 0) for i in res_inv.data) if res_inv.data else 0

        total_acumulado = total_asistencias + total_tareas

        # Respetar si la administradora hizo un ajuste manual previo
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
# 2. LOGIN Y REGISTRO
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
                        st.error("Matrícula no encontrada.")

        else:
            st.info("Ingresa tus credenciales administrativas para iniciar sesión.")
            with st.form("admin_login_form"):
                email_admin = st.text_input("Correo electrónico de la Administradora")
                password_admin = st.text_input("Contraseña", type="password")
                submit_admin = st.form_submit_button("Iniciar Sesión")

            if submit_admin:
                if not email_admin.strip() or not password_admin.strip():
                    st.warning("Por favor, ingresa correo y contraseña.")
                else:
                    try:
                        res = supabase.auth.sign_in_with_password({
                            "email": email_admin.strip(),
                            "password": password_admin.strip()
                        })
                        if res.user:
                            st.session_state["usuario_rol"] = "admin"
                            st.session_state["usuario_datos"] = {"nombre": "Administradora", "email": email_admin.strip()}
                            st.success("¡Sesión iniciada correctamente!")
                            st.rerun()
                    except Exception:
                        st.error("Credenciales incorrectas.")

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
                        
                        supabase.storage.from_("fodas").upload(
                            path=ruta_foda,
                            file=archivo_foda.getvalue(),
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

                        st.success("✅ Registro completado exitosamente.")
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
    tab_e1, tab_e2 = st.tabs(["Registro de Asistencia (Check-in / Out)", "Entregar Tareas / Investigaciones"])

    with tab_e1:
        st.subheader("Asistencia Presencial (Multiplicador x2 por tiempo)")

        res_ss = supabase.table("servicio_social").select("id_servicio, grupo_semestre").eq("id_alumno", alumno["id_alumno"]).order("id_servicio", desc=True).execute()
        if not res_ss.data:
            st.error("No estás inscrito en ningún grupo activo.")
        else:
            id_servicio = res_ss.data[0]["id_servicio"]
            grupo_semestre = res_ss.data[0]["grupo_semestre"]

            # Verificar si tiene un Check-in pendiente activo
            res_activa = supabase.table("asistencias").select("*").eq("id_alumno", alumno["id_alumno"]).eq("estado", "ACTIVA").execute()
            asistencia_pendiente = None
            
            if res_activa.data:
                # Filtramos únicamente registros que tengan una fecha válida de Check-in
                for item_asist in res_activa.data:
                    if item_asist.get("check_in_at"):
                        asistencia_pendiente = item_asist
                        break

            # Auto-cancelación si pasaron más de 8 horas desde la entrada
            if asistencia_pendiente and asistencia_pendiente.get("check_in_at"):
                try:
                    str_checkin = str(asistencia_pendiente["check_in_at"]).replace("Z", "+00:00")
                    check_in_dt = datetime.datetime.fromisoformat(str_checkin)
                    ahora_utc = datetime.datetime.now(datetime.timezone.utc)
                    
                    if (ahora_utc - check_in_dt).total_seconds() > 28800: # 8 horas
                        supabase.table("asistencias").update({"estado": "CANCELADA_EXCESO_TIEMPO", "horas_otorgadas": 0}).eq("id_asistencia", asistencia_pendiente["id_asistencia"]).execute()
                        st.warning("⚠️ Tu sesión de Check-in anterior sobrepasó las 8 horas sin marcar salida y fue cancelada. Inicia una nueva sesión.")
                        asistencia_pendiente = None
                except Exception as e_dt:
                    asistencia_pendiente = None

            if not asistencia_pendiente:
                st.info("📌 **Paso 1: Check-in (Entrada)**. Pide el código de 10 minutos a la administradora para iniciar tu jornada.")
                with st.form("form_checkin", clear_on_submit=True):
                    codigo_in = st.text_input("Código de Asistencia (Válido por 10 min)")
                    foto_in = st.camera_input("Foto de Check-in")
                    btn_in = st.form_submit_button("Registrar ENTRADA")

                if btn_in:
                    if not codigo_in or foto_in is None:
                        st.error("Ingresa el código y toma la foto de entrada.")
                    else:
                        ahora_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        res_cod = supabase.table("codigos_asistencia").select("*").eq("codigo", codigo_in.strip()).gte("expira_at", ahora_iso).execute()

                        if not res_cod.data:
                            st.error("El código es incorrecto o ya ha expirado (duración: 10 min). Solicita uno nuevo.")
                        else:
                            mat_limpia = limpiar_texto(alumno["matricula"])
                            grp_limpio = limpiar_texto(grupo_semestre)
                            str_fecha = datetime.date.today().strftime("%Y%m%d_%H%M%S")

                            ruta_in = f"expedientes/{grp_limpio}/{mat_limpia}/asistencias/{str_fecha}_checkin.jpg"
                            supabase.storage.from_("investigaciones").upload(
                                path=ruta_in,
                                file=foto_in.getvalue(),
                                file_options={"content-type": "image/jpeg", "upsert": "true"}
                            )
                            url_in = supabase.storage.from_("investigaciones").get_public_url(ruta_in)

                            supabase.table("asistencias").insert({
                                "id_alumno": alumno["id_alumno"],
                                "id_servicio": id_servicio,
                                "fecha": str(datetime.date.today()),
                                "url_foto": url_in,
                                "check_in_at": ahora_iso,
                                "estado": "ACTIVA"
                            }).execute()

                            st.success("🟢 Check-in registrado correctamente. Trabaja tus horas y al terminar registra tu Check-out.")
                            st.rerun()

            else:
                check_in_hora = datetime.datetime.fromisoformat(asistencia_pendiente["check_in_at"].replace("Z", "+00:00")).strftime("%H:%M hrs")
                st.success(f"🟢 **Check-in activo registrado a las {check_in_hora}**. Al terminar tus labores, pide el código vigente de 10 min para marcar tu salida.")
                
                with st.form("form_checkout", clear_on_submit=True):
                    codigo_out = st.text_input("Código de Salida (Válido por 10 min)")
                    foto_out = st.camera_input("Foto de Check-out (Salida)")
                    btn_out = st.form_submit_button("Registrar SALIDA")

                if btn_out:
                    if not codigo_out or foto_out is None:
                        st.error("Ingresa el código vigente y toma la foto de salida.")
                    else:
                        ahora_utc = datetime.datetime.now(datetime.timezone.utc)
                        ahora_iso = ahora_utc.isoformat()
                        res_cod = supabase.table("codigos_asistencia").select("*").eq("codigo", codigo_out.strip()).gte("expira_at", ahora_iso).execute()

                        if not res_cod.data:
                            st.error("El código es incorrecto o ha expirado. Pide un nuevo código a la administradora.")
                        else:
                            mat_limpia = limpiar_texto(alumno["matricula"])
                            grp_limpio = limpiar_texto(grupo_semestre)
                            str_fecha = datetime.date.today().strftime("%Y%m%d_%H%M%S")

                            ruta_out = f"expedientes/{grp_limpio}/{mat_limpia}/asistencias/{str_fecha}_checkout.jpg"
                            supabase.storage.from_("investigaciones").upload(
                                path=ruta_out,
                                file=foto_out.getvalue(),
                                file_options={"content-type": "image/jpeg", "upsert": "true"}
                            )
                            url_out = supabase.storage.from_("investigaciones").get_public_url(ruta_out)

                            check_in_dt = datetime.datetime.fromisoformat(asistencia_pendiente["check_in_at"].replace("Z", "+00:00"))
                            horas_ganadas, cancelado = calcular_horas_presenciales(check_in_dt, ahora_utc)

                            if cancelado:
                                supabase.table("asistencias").update({"estado": "CANCELADA_EXCESO_TIEMPO", "horas_otorgadas": 0}).eq("id_asistencia", asistencia_pendiente["id_asistencia"]).execute()
                                st.error("❌ Pasaron más de 8 horas desde la entrada. La jornada fue cancelada automáticamente.")
                            else:
                                supabase.table("asistencias").update({
                                    "foto_salida": url_out,
                                    "check_out_at": ahora_iso,
                                    "horas_otorgadas": horas_ganadas,
                                    "estado": "COMPLETADA"
                                }).eq("id_asistencia", asistencia_pendiente["id_asistencia"]).execute()

                                calcular_y_actualizar_horas(id_servicio, grupo_semestre)
                                st.balloons()
                                st.success(f"🎉 ¡Check-out registrado! Se te han asignado **{horas_ganadas} horas** a tu expediente (multiplicador x2 aplicado).")
                            st.rerun()

    with tab_e2:
        st.subheader("Entregables e Investigaciones")
        try:
            res_ss = supabase.table("servicio_social").select("id_servicio, grupo_semestre").eq("id_alumno", alumno["id_alumno"]).order("id_servicio", desc=True).execute()
            if res_ss.data:
                id_servicio = res_ss.data[0]["id_servicio"]
                grupo_actual = res_ss.data[0]["grupo_semestre"]

                res_cat = supabase.table("catalogo_entregables").select("*").eq("grupo_semestre", grupo_actual).eq("activo", True).execute()
                if not res_cat.data:
                    st.info("No hay tareas asignadas para tu grupo.")
                else:
                    mapa_tareas = {f"#{item['id_entregable']} - {item['nombre_entregable']} ({item['horas_valor']} hrs base)": item for item in res_cat.data}
                    tarea_sel_key = st.selectbox("Selecciona la Tarea a entregar:", list(mapa_tareas.keys()))
                    tarea_obj = mapa_tareas[tarea_sel_key]

                    st.write(f"**Instrucciones:** {tarea_obj.get('descripcion', 'Sin instrucciones.')}")
                    if tarea_obj.get("url_pdf_instrucciones"):
                        st.markdown(f"📄 **Guía de la tarea:** [Descargar PDF Guía]({tarea_obj['url_pdf_instrucciones']})")

                   # Cálculo dinámico de penalización por entrega tardía
                    horas_base = int(tarea_obj['horas_valor'])
                    horas_a_otorgar = horas_base
                    fecha_limite_str = tarea_obj.get("fecha_limite")
                    
                    st.markdown("---")
                    if fecha_limite_str:
                        try:
                            # 1. Convertir la fecha límite asegurando formato con zona horaria UTC
                            limite_dt = datetime.datetime.fromisoformat(fecha_limite_str.replace("Z", "+00:00"))
                            if limite_dt.tzinfo is None:
                                limite_dt = limite_dt.replace(tzinfo=datetime.timezone.utc)
                            
                            # 2. Obtener hora actual con zona horaria UTC activa
                            ahora_dt = datetime.datetime.now(datetime.timezone.utc)
                            
                            st.write(f"📅 **Fecha límite oficial:** {limite_dt.strftime('%d/%m/%Y a las %H:%M hrs')}")

                            if ahora_dt > limite_dt:
                                retraso_segundos = (ahora_dt - limite_dt).total_seconds()
                                dias_atraso = retraso_segundos / 86400.0

                                if dias_atraso <= 1.0: # Hasta 1 día de retraso (24 hrs)
                                    horas_a_otorgar = max(0, horas_base - 2)
                                    st.warning(f"⚠️ **Entrega con retraso (Menos de 24 hrs):** Se aplican -2 horas de penalización. Esta entrega valdrá **{horas_a_otorgar} hrs**.")
                                elif dias_atraso <= 2.0: # Hasta 2 días de retraso (48 hrs)
                                    horas_a_otorgar = int(horas_base * 0.5)
                                    st.warning(f"⚠️ **Entrega con 2 días de retraso:** Vale la mitad de los puntos. Esta entrega valdrá **{horas_a_otorgar} hrs**.")
                                else: # Más de 2 días (>48 hrs)
                                    horas_a_otorgar = 0
                                    st.error("❌ **Entrega con más de 2 días de retraso:** Puedes subir tu tarea para revisión de evidencia, pero otorgará **0 horas**.")
                            else:
                                st.info(f"✅ **Entrega a tiempo:** Esta tarea otorgará **{horas_a_otorgar} hrs** completas.")
                        except Exception as e_dt:
                            st.warning(f"Fecha límite registrada: {fecha_limite_str}")

                    archivo_tarea = st.file_uploader("Adjuntar archivo PDF de tu respuesta", type=["pdf"])
                    if st.button("Enviar Tarea"):
                        if archivo_tarea is None:
                            st.error("Adjunta el archivo PDF.")
                        else:
                            mat_limpia = limpiar_texto(alumno["matricula"])
                            grp_limpio = limpiar_texto(grupo_actual)
                            nom_tarea_limpia = limpiar_texto(tarea_obj['nombre_entregable'])

                            ruta_tarea = f"expedientes/{grp_limpio}/{mat_limpia}/entregables/{tarea_obj['id_entregable']}_{nom_tarea_limpia}.pdf"
                            supabase.storage.from_("investigaciones").upload(
                                path=ruta_tarea,
                                file=archivo_tarea.getvalue(),
                                file_options={"content-type": "application/pdf", "upsert": "true"}
                            )
                            url_tarea = supabase.storage.from_("investigaciones").get_public_url(ruta_tarea)

                            supabase.table("investigaciones").delete().eq("id_servicio", id_servicio).eq("id_entregable", tarea_obj['id_entregable']).execute()

                            supabase.table("investigaciones").insert({
                                "id_alumno": alumno["id_alumno"],
                                "id_servicio": id_servicio,
                                "id_entregable": tarea_obj['id_entregable'],
                                "titulo_entrega": tarea_obj['nombre_entregable'],
                                "url_pdf": url_tarea,
                                "horas_ganadas": horas_a_otorgar
                            }).execute()

                            calcular_y_actualizar_horas(id_servicio, grupo_actual)
                            st.success(f"✅ Tarea enviada exitosamente. Se asignaron {horas_a_otorgar} hrs a tu expediente.")
                            st.rerun()
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

    grupos_list = []
    try:
        res_g_all = supabase.table("grupos").select("nombre_grupo").order("nombre_grupo").execute()
        if res_g_all.data:
            grupos_list = [g["nombre_grupo"] for g in res_g_all.data]
    except Exception:
        grupos_list = []

    if grupos_list:
        grupo_activo = st.selectbox("Selecciona el Grupo de Trabajo activo:", grupos_list)
    else:
        grupo_activo = None
        st.warning("⚠️ Crea un grupo de trabajo en la pestaña 'Configuración y Grupos'.")

    tab_a1, tab_a2, tab_a3 = st.tabs([
        "Alumnos y Expediente", 
        "Asistencias y Revisiones de Tareas", 
        "⚙️ Generar Código y Configuración"
    ])

    # ---------------------------------------------------------
    # TAB ADMIN 1: ALUMNOS, MODIFICACIÓN MANUAL DE HORAS Y ZIP
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
                    column_config={"FODA": st.column_config.LinkColumn("FODA PDF", display_text="📄 Ver PDF")},
                    use_container_width=True
                )

                st.markdown("---")
                col_exp1, col_exp2 = st.columns(2)

                # --- EDICIÓN Y AJUSTE MANUAL DE HORAS DEL ESTUDIANTE ---
                with col_exp1:
                    st.subheader("Editar Datos u Horas del Estudiante")
                    alumno_sel_edit = st.selectbox("Selecciona Alumno:", list(opciones_alumnos.keys()))
                    reg_edit = opciones_alumnos[alumno_sel_edit]
                    al_data_edit = reg_edit.get("alumnos", {}) or {}

                    with st.expander("Formulario de Edición y Ajuste de Horas"):
                        new_nom = st.text_input("Nombre Completo:", value=al_data_edit.get("nombre", ""))
                        new_mat = st.text_input("Matrícula:", value=al_data_edit.get("matricula", ""))
                        new_car = st.text_input("Carrera:", value=al_data_edit.get("carrera", ""))
                        
                        st.markdown("---")
                        st.write("✏️ **Ajuste Manual de Horas Acumuladas:**")
                        new_horas = st.number_input("Horas Totales del Alumno:", value=int(reg_edit.get("horas_acumuladas", 0)), min_value=0)

                        col_btn1, col_btn2 = st.columns(2)
                        with col_btn1:
                            if st.button("Guardar Cambios y Horas"):
                                supabase.table("alumnos").update({
                                    "nombre": new_nom,
                                    "matricula": new_mat,
                                    "carrera": new_car
                                }).eq("id_alumno", al_data_edit["id_alumno"]).execute()

                                supabase.table("servicio_social").update({
                                    "horas_acumuladas": new_horas
                                }).eq("id_servicio", reg_edit["id_servicio"]).execute()

                                st.success("Datos y horas del estudiante actualizados.")
                                st.rerun()
                        
                        with col_btn2:
                            if st.button("Eliminar Alumno"):
                                supabase.table("servicio_social").delete().eq("id_servicio", reg_edit["id_servicio"]).execute()
                                supabase.table("alumnos").delete().eq("id_alumno", al_data_edit["id_alumno"]).execute()
                                st.success("Registro eliminado.")
                                st.rerun()

                # --- DESCARGA ZIP (TOMA SOLO LA FOTO DE INICIO CHECK-IN) ---
                with col_exp2:
                    st.subheader("Descargar Expedientes ZIP")
                    st.write("Descarga los archivos. En asistencias solo incluye la **foto de inicio (Check-in)**.")

                    if st.button("Generar Archivo ZIP de Evidencias"):
                        with st.spinner("Compilando carpetas y fotos de check-in..."):
                            zip_buffer = io.BytesIO()

                            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                                for reg in res_ss.data:
                                    al_info = reg.get("alumnos", {}) or {}
                                    mat = limpiar_texto(al_info.get("matricula", "desconocido"))
                                    nom = limpiar_texto(al_info.get("nombre", "alumno"))
                                    id_serv = reg["id_servicio"]

                                    carpeta_estudiante = f"{mat}_{nom}"

                                    # Solo descargamos url_foto (Foto de Entrada / Check-in)
                                    res_fotos = supabase.table("asistencias").select("fecha, url_foto").eq("id_servicio", id_serv).execute()
                                    if res_fotos.data:
                                        for idx, f in enumerate(res_fotos.data):
                                            url_f = f.get("url_foto")
                                            if url_f:
                                                try:
                                                    resp = requests.get(url_f)
                                                    if resp.status_code == 200:
                                                        fecha_f = f.get("fecha", f"checkin_{idx}")
                                                        nom_foto = f"{fecha_f}_{mat}_checkin.jpg"
                                                        ruta_en_zip = f"{carpeta_estudiante}/Asistencias_CheckIn/{nom_foto}"
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
    # TAB ADMIN 2: REVISIÓN DE ASISTENCIAS Y TAREAS
    # ---------------------------------------------------------
    with tab_a2:
        if grupo_activo:
            st.subheader(f"Asistencias y Tareas - {grupo_activo}")
            sub_tab1, sub_tab2 = st.tabs(["Registro de Check-in / Out del Día", "Revisión de Tareas"])

            with sub_tab1:
                hoy_str = str(datetime.date.today())
                st.write("### Evidencias de Asistencia Presencial Capturadas Hoy")

                res_asist_fotos = supabase.table("asistencias").select(
                    "fecha, url_foto, foto_salida, horas_otorgadas, estado, alumnos(matricula, nombre)"
                ).eq("fecha", hoy_str).order("id_asistencia", desc=True).execute()

                if res_asist_fotos.data:
                    cols = st.columns(3)
                    for idx, item in enumerate(res_asist_fotos.data):
                        al_data = item.get("alumnos", {}) or {}
                        with cols[idx % 3]:
                            st.caption(f"**{al_data.get('nombre')}** ({item['estado']}) - {item.get('horas_otorgadas', 0)} hrs ganadas")
                            c1, c2 = st.columns(2)
                            with c1:
                                if item.get("url_foto"):
                                    st.image(item["url_foto"], caption="Check-in", use_container_width=True)
                            with c2:
                                if item.get("foto_salida"):
                                    st.image(item["foto_salida"], caption="Check-out", use_container_width=True)
                else:
                    st.info("No hay registros de asistencia presencial capturados el día de hoy.")

            with sub_tab2:
                st.write("### Consultar Tareas Entregadas por Estudiantes")
                res_cat_t = supabase.table("catalogo_entregables").select("id_entregable, nombre_entregable").eq("grupo_semestre", grupo_activo).execute()
                
                if not res_cat_t.data:
                    st.info("No se han publicado tareas para este grupo.")
                else:
                    mapa_tareas_rev = {f"#{t['id_entregable']} - {t['nombre_entregable']}": t for t in res_cat_t.data}
                    tarea_filtro_key = st.selectbox("Selecciona la tarea a revisar:", list(mapa_tareas_rev.keys()))
                    tarea_filtro_obj = mapa_tareas_rev[tarea_filtro_key]

                    res_entregas = supabase.table("investigaciones").select(
                        "created_at, url_pdf, horas_ganadas, alumnos(matricula, nombre)"
                    ).eq("id_entregable", tarea_filtro_obj["id_entregable"]).execute()

                    if res_entregas.data:
                        tabla_rev = []
                        for ent in res_entregas.data:
                            al_info = ent.get("alumnos", {}) or {}
                            tabla_rev.append({
                                "Matrícula": al_info.get("matricula"),
                                "Alumno": al_info.get("nombre"),
                                "Fecha Entrega": ent.get("created_at", "")[:10],
                                "Horas Ganadas": f"{ent.get('horas_ganadas', 0)} hrs",
                                "PDF": ent.get("url_pdf")
                            })

                        st.dataframe(
                            tabla_rev,
                            column_config={"PDF": st.column_config.LinkColumn("Documento PDF", display_text="📄 Ver / Descargar PDF")},
                            use_container_width=True
                        )
                    else:
                        st.warning("Ningún alumno ha subido respuesta para esta tarea aún.")

    # ---------------------------------------------------------
    # TAB ADMIN 3: GENERADOR DE CÓDIGO DE 10 MIN Y TAREAS
    # ---------------------------------------------------------
    with tab_a3:
        st.subheader("🔑 Generador de Código de 10 Minutos y Tareas")
        
        # --- GENERADOR DE CÓDIGOS DE 10 MINUTOS ---
        st.markdown("### ⏱️ Código de Asistencia Dinámico (Expira en 10 minutos)")
        
        col_c1, col_c2 = st.columns([2, 1])
        with col_c1:
            ahora_utc = datetime.datetime.now(datetime.timezone.utc)
            res_cod_act = supabase.table("codigos_asistencia").select("*").gte("expira_at", ahora_utc.isoformat()).order("id_codigo", desc=True).execute()

            if res_cod_act.data:
                codigo_vigente = res_cod_act.data[0]
                exp_dt = datetime.datetime.fromisoformat(codigo_vigente["expira_at"].replace("Z", "+00:00"))
                min_restantes = max(0, int((exp_dt - ahora_utc).total_seconds() // 60))
                
                st.metric("Código Activo:", codigo_vigente["codigo"], help=f"Expira en {min_restantes} min")
                st.info(f"⏳ **Válido por los próximos {min_restantes} minutos** (Expira a las {exp_dt.strftime('%H:%M:%S')} UTC). Proporciónalo a los alumnos para Check-in o Check-out.")
            else:
                st.warning("No hay ningún código activo en este momento.")

        with col_c2:
            st.write("")
            if st.button("🔑 Generar Código de 10 Minutos"):
                nuevo_num = str(random.randint(1000, 9999))
                expira_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)

                supabase.table("codigos_asistencia").insert({
                    "fecha": str(datetime.date.today()),
                    "codigo": nuevo_num,
                    "expira_at": expira_dt.isoformat()
                }).execute()

                st.success(f"¡Nuevo código generado: **{nuevo_num}**!")
                st.rerun()

        st.markdown("---")

        # --- GESTIÓN DE GRUPOS Y TAREAS ---
        col_g1, col_g2 = st.columns(2)
        
        with col_g1:
            st.write("### Crear Grupo / Cerrar Semestre")
            nuevo_grp = st.text_input("Nombre del Nuevo Grupo (Ej. Semestre 2026-2)")
            if st.button("Crear Grupo"):
                if nuevo_grp.strip():
                    supabase.table("grupos").insert({"nombre_grupo": nuevo_grp.strip(), "estado_grupo": "En Proceso"}).execute()
                    st.success("Grupo creado.")
                    st.rerun()

            st.markdown("---")
            if grupo_activo and st.button("Marcar Grupo Completo como Concluido"):
                supabase.table("servicio_social").update({"estado": "Concluido"}).eq("grupo_semestre", grupo_activo).execute()
                supabase.table("grupos").update({"estado_grupo": "Concluido"}).eq("nombre_grupo", grupo_activo).execute()
                st.success(f"El grupo '{grupo_activo}' ha sido marcado como Concluido.")
                st.rerun()

        with col_g2:
            st.write("### Gestión y Edición de Tareas")
            if grupo_activo:
                accion_tarea = st.radio("Acción de Tareas:", ["➕ Crear Nueva Tarea", "✏️ Editar/Eliminar Tarea"], horizontal=True)

                if accion_tarea == "➕ Crear Nueva Tarea":
                    nom_tarea = st.text_input("Nombre de la Tarea")
                    hrs_tarea = st.number_input("Horas Base que Otorga", min_value=1, value=10)
                    desc_tarea = st.text_area("Instrucciones de la tarea")
                    
                    pdf_instruccion = st.file_uploader("Adjuntar PDF con instrucciones/guía (Opcional)", type=["pdf"])

                    c_f, c_h = st.columns(2)
                    with c_f:
                        f_limite = st.date_input("Fecha límite", value=datetime.date.today() + datetime.timedelta(days=7))
                    with c_h:
                        h_limite = st.time_input("Hora límite", value=datetime.time(23, 59))

                    if st.button("Asignar Tarea al Grupo"):
                        if nom_tarea.strip():
                            url_pdf_guia = None
                            if pdf_instruccion is not None:
                                nom_guia_limpio = limpiar_texto(nom_tarea)
                                ruta_guia = f"guias_tareas/{limpiar_texto(grupo_activo)}/{nom_guia_limpio}_guia.pdf"
                                supabase.storage.from_("investigaciones").upload(
                                    path=ruta_guia,
                                    file=pdf_instruccion.getvalue(),
                                    file_options={"content-type": "application/pdf", "upsert": "true"}
                                )
                                url_pdf_guia = supabase.storage.from_("investigaciones").get_public_url(ruta_guia)

                            dt_combinado = datetime.datetime.combine(f_limite, h_limite)
                            iso_limite = dt_combinado.isoformat()

                            supabase.table("catalogo_entregables").insert({
                                "grupo_semestre": grupo_activo,
                                "nombre_entregable": nom_tarea.strip(),
                                "horas_valor": int(hrs_tarea),
                                "descripcion": desc_tarea.strip(),
                                "url_pdf_instrucciones": url_pdf_guia,
                                "fecha_limite": iso_limite,
                                "activo": True
                            }).execute()
                            st.success(f"Tarea '{nom_tarea}' creada correctamente.")
                            st.rerun()

                else:
                    res_cat_edit = supabase.table("catalogo_entregables").select("*").eq("grupo_semestre", grupo_activo).execute()
                    if not res_cat_edit.data:
                        st.info("No hay tareas creadas en este grupo para modificar.")
                    else:
                        mapa_edit = {f"#{t['id_entregable']} - {t['nombre_entregable']}": t for t in res_cat_edit.data}
                        tarea_edit_key = st.selectbox("Selecciona la tarea a gestionar:", list(mapa_edit.keys()))
                        tarea_edit_obj = mapa_edit[tarea_edit_key]

                        with st.form("form_edit_tarea"):
                            st.write(f"**Modificar Tarea #{tarea_edit_obj['id_entregable']}**")
                            edit_nom = st.text_input("Nombre de la Tarea:", value=tarea_edit_obj["nombre_entregable"])
                            edit_hrs = st.number_input("Horas que Otorga:", min_value=1, value=int(tarea_edit_obj["horas_valor"]))
                            edit_desc = st.text_area("Instrucciones:", value=tarea_edit_obj.get("descripcion", ""))
                            edit_pdf = st.file_uploader("Actualizar/Subir PDF guía (Opcional)", type=["pdf"])

                            c_b1, c_b2 = st.columns(2)
                            with c_b1:
                                btn_guardar_edit = st.form_submit_button("💾 Guardar Cambios")
                            with c_b2:
                                btn_eliminar_tarea = st.form_submit_button("🗑️ Eliminar Tarea")

                        if btn_guardar_edit:
                            url_pdf_edit = tarea_edit_obj.get("url_pdf_instrucciones")
                            if edit_pdf is not None:
                                nom_guia_limpio = limpiar_texto(edit_nom)
                                ruta_guia = f"guias_tareas/{limpiar_texto(grupo_activo)}/{tarea_edit_obj['id_entregable']}_{nom_guia_limpio}_guia.pdf"
                                supabase.storage.from_("investigaciones").upload(
                                    path=ruta_guia,
                                    file=edit_pdf.getvalue(),
                                    file_options={"content-type": "application/pdf", "upsert": "true"}
                                )
                                url_pdf_edit = supabase.storage.from_("investigaciones").get_public_url(ruta_guia)

                            supabase.table("catalogo_entregables").update({
                                "nombre_entregable": edit_nom.strip(),
                                "horas_valor": int(edit_hrs),
                                "descripcion": edit_desc.strip(),
                                "url_pdf_instrucciones": url_pdf_edit
                            }).eq("id_entregable", tarea_edit_obj["id_entregable"]).execute()

                            st.success("✅ Tarea actualizada correctamente.")
                            st.rerun()

                        if btn_eliminar_tarea:
                            id_ent_borrar = tarea_edit_obj["id_entregable"]
                            supabase.table("investigaciones").delete().eq("id_entregable", id_ent_borrar).execute()
                            supabase.table("catalogo_entregables").delete().eq("id_entregable", id_ent_borrar).execute()

                            res_ss_grp = supabase.table("servicio_social").select("id_servicio, grupo_semestre").eq("grupo_semestre", grupo_activo).execute()
                            if res_ss_grp.data:
                                for item_ss in res_ss_grp.data:
                                    calcular_y_actualizar_horas(item_ss["id_servicio"], grupo_activo)

                            st.success("🗑️ Tarea eliminada y horas recalculadas.")
                            st.rerun()
