from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file

from .nuvem import (
    ErroDeCopia,
    ErroDeSincronizacao,
    UpdateError,
    check_release,
    download_release,
    exportar_copia,
    importar_copia,
    prepare_installer,
    read_version,
    enviar,
    estado_publico,
    gravar_config,
    olhar_la,
    receber,
    resumo_do_registro,
    start_installer,
)
from .paths import BASE_DIR
from .service import ControleService


def create_app(service: ControleService | None = None) -> Flask:
    svc = service or ControleService()
    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates"),
        static_folder=str(BASE_DIR / "static"),
    )
    app.config["controle_service"] = svc

    def ok(data: Any = None, **extra: Any):
        payload = {"ok": True}
        if data is not None:
            payload["data"] = data
        payload.update(extra)
        return jsonify(payload)

    def fail(exc: Exception, status: int = 400):
        return jsonify({"ok": False, "message": str(exc)}), status

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/favicon.ico")
    def favicon():
        return send_file(BASE_DIR / "static" / "img" / "favicon.ico", mimetype="image/vnd.microsoft.icon")

    @app.get("/api/state")
    def state():
        return ok(svc.state())

    @app.get("/api/projects")
    def list_projects():
        return ok(svc.db.list_projects(search=request.args.get("search", ""), status=request.args.get("status", "")))

    @app.post("/api/projects")
    def create_project():
        try:
            return ok(svc.create_project(request.get_json(force=True) or {}))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/projects/<int:project_id>")
    def get_project(project_id: int):
        project = svc.db.get_project(project_id)
        if not project:
            return fail(ValueError("Projeto não encontrado."), 404)
        return ok(project)

    @app.put("/api/projects/<int:project_id>")
    def update_project(project_id: int):
        try:
            return ok(svc.update_project(project_id, request.get_json(force=True) or {}))
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/projects/<int:project_id>")
    def delete_project(project_id: int):
        try:
            svc.delete_project(project_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<int:project_id>/activate")
    def activate_project(project_id: int):
        try:
            return ok(svc.set_active_project(project_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<int:project_id>/status")
    def project_status(project_id: int):
        try:
            data = request.get_json(force=True) or {}
            return ok(svc.set_project_status(project_id, str(data.get("status") or "")))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<int:project_id>/raise-revision")
    def raise_project_revision(project_id: int):
        try:
            data = request.get_json(force=True) or {}
            return ok(svc.raise_project_revision(
                project_id,
                str(data.get("revision") or ""),
                float(data.get("hours") or 0),
                float(data.get("minutes") or 0),
                str(data.get("revision_date") or ""),
            ))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/projects/<int:project_id>/stages/<int:stage_order>/completion-plan")
    def stage_completion_plan(project_id: int, stage_order: int):
        try:
            return ok(svc.db.stage_completion_plan(project_id, stage_order))
        except Exception as exc:
            return fail(exc)

    @app.patch("/api/projects/<int:project_id>/stages/<int:stage_order>")
    def update_stage(project_id: int, stage_order: int):
        try:
            return ok(svc.db.update_stage(project_id, stage_order, request.get_json(force=True) or {}))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<int:project_id>/stages/reopen")
    def reopen_stages(project_id: int):
        try:
            data = request.get_json(force=True) or {}
            return ok(svc.db.reopen_stages(project_id, data.get("stage_orders") or []))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/projects/<int:project_id>/stages")
    def replace_project_stages(project_id: int):
        try:
            data = request.get_json(force=True) or {}
            return ok(svc.replace_project_stages(project_id, data.get("stages") or []))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/projects/<int:project_id>/stage-times")
    def adjust_stage_times(project_id: int):
        try:
            data = request.get_json(force=True) or {}
            return ok(svc.db.adjust_stage_times(project_id, data.get("stages") or []))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<int:project_id>/finalize")
    def finalize_project(project_id: int):
        try:
            return ok(svc.finalize_project(project_id))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/projects/<int:project_id>/indicators")
    def get_project_indicators(project_id: int):
        try:
            return ok(svc.db.get_project_indicators(project_id))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/projects/<int:project_id>/indicators")
    def save_project_indicators(project_id: int):
        try:
            return ok(svc.save_project_indicators(project_id, request.get_json(force=True) or {}))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<int:project_id>/indicators/import-pdf")
    def import_project_indicators(project_id: int):
        try:
            data = request.get_json(force=True) or {}
            return ok(svc.import_project_indicators(
                project_id, str(data.get("path") or ""), bool(data.get("confirm"))
            ))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/stage-templates")
    def get_stage_template():
        try:
            program = request.args.get("program", "")
            mode = request.args.get("mode", "")
            return ok(svc.get_stage_template(program, mode))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/stage-templates")
    def replace_stage_template():
        try:
            data = request.get_json(force=True) or {}
            return ok(svc.replace_stage_template(str(data.get("program") or ""), str(data.get("mode") or ""), data.get("stages") or []))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/maintenance/reset")
    def reset_operational_data():
        try:
            return ok(svc.reset_operational_data())
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<int:project_id>/issues")
    def create_issue(project_id: int):
        try:
            return ok(svc.db.save_issue(project_id, request.get_json(force=True) or {}))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/projects/<int:project_id>/issues/<int:issue_id>")
    def update_issue(project_id: int, issue_id: int):
        try:
            return ok(svc.db.save_issue(project_id, request.get_json(force=True) or {}, issue_id=issue_id))
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/projects/<int:project_id>/issues/<int:issue_id>")
    def delete_issue(project_id: int, issue_id: int):
        try:
            return ok(svc.db.delete_issue(project_id, issue_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<int:project_id>/files")
    def create_file(project_id: int):
        try:
            return ok(svc.db.save_file(project_id, request.get_json(force=True) or {}))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/projects/<int:project_id>/files/<int:file_id>")
    def update_file(project_id: int, file_id: int):
        try:
            return ok(svc.db.save_file(project_id, request.get_json(force=True) or {}, file_id=file_id))
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/projects/<int:project_id>/files/<int:file_id>")
    def delete_file(project_id: int, file_id: int):
        try:
            return ok(svc.db.delete_file(project_id, file_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<int:project_id>/drawings")
    def create_drawing(project_id: int):
        try:
            return ok(svc.db.save_drawing(project_id, request.get_json(force=True) or {}))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/projects/<int:project_id>/drawings/<int:drawing_id>")
    def update_drawing(project_id: int, drawing_id: int):
        try:
            return ok(svc.db.save_drawing(project_id, request.get_json(force=True) or {}, drawing_id=drawing_id))
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/projects/<int:project_id>/drawings/<int:drawing_id>")
    def delete_drawing(project_id: int, drawing_id: int):
        try:
            return ok(svc.db.delete_drawing(project_id, drawing_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<int:project_id>/time")
    def record_time(project_id: int):
        try:
            data = request.get_json(force=True) or {}
            hours = float(data.get("hours") or 0)
            minutes = float(data.get("minutes") or 0)
            seconds = int((hours * 3600) + (minutes * 60))
            return ok(svc.db.record_time(project_id, int(data.get("stage_order") or 1), seconds, str(data.get("note") or ""), "manual"))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/settings")
    def settings():
        try:
            return ok(svc.update_settings(request.get_json(force=True) or {}))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/weights")
    def weights():
        try:
            data = request.get_json(force=True) or {}
            return ok(svc.db.update_weight_profile(data.get("weights") or {}, str(data.get("name") or "Perfil padrão provisório")))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/projects/<int:project_id>/recalibration")
    def recalibration(project_id: int):
        try:
            return ok(svc.db.recalibration_suggestion(project_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/open-path")
    def open_path():
        try:
            data = request.get_json(force=True) or {}
            return ok(svc.open_path(str(data.get("path") or ""), bool(data.get("folder"))))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/progresso-detalhamento/open")
    def open_progresso_detalhamento():
        try:
            return ok(svc.open_progresso_detalhamento())
        except Exception as exc:
            return fail(exc)

    @app.post("/api/choose-path")
    def choose_path():
        try:
            data = request.get_json(force=True) or {}
            return ok(svc.choose_path(
                bool(data.get("folder")),
                str(data.get("initial_dir") or ""),
                str(data.get("project_code") or ""),
            ))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/exports/projects.csv")
    def export_projects():
        path = svc.export_projects()
        return send_file(path, as_attachment=True, download_name=Path(path).name)

    @app.get("/api/exports/activity.csv")
    def export_activity():
        path = svc.export_activity()
        return send_file(path, as_attachment=True, download_name=Path(path).name)

    @app.get("/api/reports/<report_type>")
    def export_report(report_type: str):
        path = svc.export_report(report_type)
        return send_file(path, as_attachment=True, download_name=Path(path).name)

    # ---------------------------------------------------- copia de seguranca

    @app.get("/api/copia/resumo")
    def copia_resumo():
        try:
            return ok(resumo_do_registro())
        except Exception as exc:
            return fail(exc)

    @app.get("/api/copia/exportar")
    def copia_exportar():
        try:
            dados, nome = exportar_copia()
        except ErroDeCopia as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 500)
        return Response(
            dados,
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{nome}"'},
        )

    # ------------------------------------------------- atualizar o programa
    # A release pendente fica aqui entre o "verificar" e o "instalar": instalar
    # sem ter verificado antes nao acontece, e a versao instalada e sempre a que
    # o usuario viu na tela.
    atualizacao_pendente: dict[str, Any] = {}

    @app.get("/api/atualizacao/estado")
    def atualizacao_estado():
        try:
            atual = read_version(BASE_DIR)
            return ok({
                "versao": atual.get("version", ""),
                "repositorio": atual.get("repository", ""),
                "pendente": getattr(atualizacao_pendente.get("release"), "version", ""),
            })
        except Exception as exc:
            return fail(exc)

    @app.post("/api/atualizacao/verificar")
    def atualizacao_verificar():
        try:
            atual = read_version(BASE_DIR)
            release = check_release(atual)
        except UpdateError as exc:
            # Nao ter release ainda nao e falha: e so nao haver o que baixar.
            # Devolver isso como erro pintava a tela de vermelho a toa.
            if "nao publicado" in str(exc).lower() or "não publicado" in str(exc).lower():
                return ok({"disponivel": False, "publicado": False,
                           "versao": read_version(BASE_DIR).get("version", "")})
            return fail(exc, 502)
        except Exception as exc:
            return fail(exc, 502)
        atualizacao_pendente.clear()
        if release is None:
            return ok({"disponivel": False, "publicado": True, "versao": atual.get("version", "")})
        atualizacao_pendente["release"] = release
        return ok({"disponivel": True, "versao": release.version, "atual": atual.get("version", "")})

    @app.post("/api/atualizacao/instalar")
    def atualizacao_instalar():
        release = atualizacao_pendente.get("release")
        if release is None:
            return fail(ValueError("Procure uma versão nova antes de instalar."))
        try:
            pacote = download_release(release, BASE_DIR)
            plano = prepare_installer(pacote, BASE_DIR, release)
            start_installer(plano)
        except UpdateError as exc:
            return fail(exc, 502)
        except OSError as exc:
            return fail(exc, 500)
        atualizacao_pendente.clear()
        return ok({"instalando": True, "versao": release.version})

    @app.post("/api/copia/importar")
    def copia_importar():
        enviado = request.files.get("arquivo")
        if enviado is None:
            return fail(ValueError("Escolha o arquivo da cópia."))
        try:
            return ok(importar_copia(enviado.read(), svc.db))
        except ErroDeCopia as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 500)

    # ----------------------------------------------------------- sincronizacao

    @app.get("/api/sincronizacao/estado")
    def sincronizacao_estado():
        try:
            return ok(estado_publico())
        except Exception as exc:
            return fail(exc)

    @app.put("/api/sincronizacao/config")
    def sincronizacao_config():
        dados = request.get_json(force=True) or {}
        novo: dict[str, Any] = {
            "ligada": bool(dados.get("ligada")),
            "repositorio": str(dados.get("repositorio") or "").strip(),
            "maquina": str(dados.get("maquina") or "").strip(),
        }
        # Token vazio nao apaga o que ja esta gravado: a tela nunca o recebe de
        # volta, entao salvar o formulario sem redigita-lo nao pode limpa-lo.
        token = str(dados.get("token") or "").strip()
        if token:
            novo["token"] = token
        try:
            gravar_config(novo)
            return ok(estado_publico())
        except Exception as exc:
            return fail(exc)

    @app.get("/api/sincronizacao/olhar")
    def sincronizacao_olhar():
        try:
            return ok(olhar_la())
        except ErroDeSincronizacao as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 502)

    @app.post("/api/sincronizacao/enviar")
    def sincronizacao_enviar():
        try:
            return ok(enviar())
        except ErroDeSincronizacao as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 502)

    @app.post("/api/sincronizacao/receber")
    def sincronizacao_receber():
        dados = request.get_json(silent=True) or {}
        try:
            return ok(receber(svc.db, bool(dados.get("forcar"))))
        except ErroDeSincronizacao as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 502)

    return app
