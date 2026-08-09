// Smallest useful Express app — used to exercise the Hangar deploy pipeline.
const express = require("express");
const os = require("os");

const app = express();
const port = process.env.PORT || 3000;

app.get("/", (req, res) => {
  res.json({
    app: "express-hello",
    message: "Deployed by Hangar.",
    node: process.version,
    hostname: os.hostname(),
    deployed_by: process.env.HANGAR_APP_NAME || "unknown",
  });
});

app.get("/health", (req, res) => {
  res.json({ status: "ok" });
});

app.listen(port, "0.0.0.0", () => {
  console.log(`express-hello listening on ${port}`);
});
