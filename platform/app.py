import os
from flask import Flask, render_template, jsonify

from config import Config
from app.services.workflow_service import UploadError


def create_app():
    app = Flask(__name__, template_folder="app/templates", static_folder="app/static")
    app.config.from_object(Config)

    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    os.makedirs(app.config["REPORTS_DIR"], exist_ok=True)
    os.makedirs(app.config["GRAPHS_DIR"], exist_ok=True)

    from app.routes.upload_routes import bp as upload_bp
    from app.routes.visualization_routes import bp as visualization_bp
    from app.routes.implementation_routes import bp as implementation_bp
    from app.routes.report_routes import bp as report_bp
    from app.routes.field_lineage_routes import bp as field_lineage_bp
    from app.routes.reportability_routes import bp as reportability_bp

    app.register_blueprint(upload_bp)
    app.register_blueprint(visualization_bp)
    app.register_blueprint(implementation_bp)
    app.register_blueprint(report_bp)
    app.register_blueprint(field_lineage_bp)
    app.register_blueprint(reportability_bp)

    from app.services.workflow_service import get_filename

    @app.context_processor
    def inject_globals():
        return {"filename": get_filename()}


    @app.errorhandler(UploadError)
    def handle_upload_error(e):
        from flask import request
        api_prefixes = ("/graph", "/session", "/transformation", "/report/")
        if request.path.startswith(api_prefixes):
            return jsonify({"error": {"code": e.code, "message": e.message}}), 400
        return render_template("error.html", message=e.message), 400

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", message="Page not found."), 404

    @app.errorhandler(413)
    def too_large(e):
        return jsonify({"error": {"code": "FILE_TOO_LARGE", "message": "Uploaded file exceeds the size limit."}}), 413

    @app.errorhandler(500)
    def server_error(e):
        return render_template("error.html", message="An unexpected server error occurred."), 500

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
