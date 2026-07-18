@app.get("/health")               # FastAPI/Flask route decorator -> entry point
def health():
    pass


@app.route("/users", methods=["POST"])
def create_user():
    pass
