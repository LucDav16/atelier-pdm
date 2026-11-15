//! Atelier PDM plugin for SolidCraft - a starting point. See README.md.
//! Everything except the two `HOST:` places is independent of SolidCraft's plugin interface.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// The folder shared with Atelier PDM. The user grants the plugin access to it.
pub const BRIDGE: &str = r"C:\Design Files\_Atelier\plugin";

#[derive(Serialize)]
pub struct Request<'a> {
    pub id: String,
    pub action: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub file: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub comment: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub folder: Option<&'a str>,
}

#[derive(Deserialize, Debug, Default)]
pub struct Response {
    pub ok: bool,
    pub message: String,
    #[serde(default)]
    pub revision: String,
    #[serde(default)]
    pub state: String,
}

fn new_id() -> String {
    let t = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0);
    format!("sc-{t}")
}

/// Writes a request, then waits up to `wait` for Atelier's answer.
pub fn ask(bridge: &Path, req: &Request, wait: Duration) -> Response {
    let requests = bridge.join("requests");
    let responses = bridge.join("responses");
    let _ = fs::create_dir_all(&requests);
    let tmp: PathBuf = requests.join(format!("{}.tmp", req.id));
    let body = serde_json::to_vec_pretty(req).unwrap_or_default();
    if fs::write(&tmp, body).is_err() || fs::rename(&tmp, requests.join(format!("{}.json", req.id))).is_err() {
        return Response { ok: false, message: format!("Can't write to {} - allow the plugin this folder.", bridge.display()), ..Default::default() };
    }
    let answer = responses.join(format!("{}.json", req.id));
    let start = Instant::now();
    while start.elapsed() < wait {
        if let Ok(text) = fs::read_to_string(&answer) {
            let _ = fs::remove_file(&answer);
            return serde_json::from_str(&text).unwrap_or(Response { ok: false, message: "Unreadable answer from Atelier.".into(), ..Default::default() });
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    Response { ok: false, message: "Atelier PDM didn't answer - is Atelier (or its panel) open?".into(), ..Default::default() }
}

/// One menu command. `file` is the open design's path; `save` runs SolidCraft's Save first.
pub fn run(action: &str, file: Option<&str>, comment: Option<&str>, folder: Option<&str>, save: &dyn Fn() -> bool) -> Response {
    if matches!(action, "check_in" | "save_to_vault") && !save() {
        return Response { ok: false, message: "Couldn't save the design - save it, then try again.".into(), ..Default::default() };
    }
    let req = Request { id: new_id(), action, file, comment, folder };
    ask(Path::new(BRIDGE), &req, Duration::from_secs(20))
}

// ---------------------------------------------------------------------------------------------------------------
// HOST: connect to SolidCraft's plugin interface (docs/plugins.md). The plugin needs to:
//   1. register the commands  "Atelier: Status", "Atelier: Check out", "Atelier: Check in",
//      "Atelier: Undo check-out", "Atelier: Save to vault"  (Check in / Save to vault ask for an optional comment
//      / vault folder in the command's settings dialog);
//   2. when one runs: get the open design's file path, call `run(...)` with a closure that runs SolidCraft's Save
//      command, and show `Response.message` to the user (and `revision` / `state` for Status).
// Example shape (names are placeholders - replace with the real interface):
//
//   #[export]                      // HOST: the plugin entry point
//   fn command(id: &str, params: &str) -> String {
//       let file = host::document_path();                                  // HOST
//       let save = || host::execute("file.save", "{}").is_ok();            // HOST
//       let r = match id {
//           "atelier.status"   => run("status", file.as_deref(), None, None, &|| true),
//           "atelier.checkout" => run("check_out", file.as_deref(), None, None, &|| true),
//           "atelier.checkin"  => run("check_in", file.as_deref(), Some(params), None, &save),
//           "atelier.undo"     => run("undo", file.as_deref(), None, None, &|| true),
//           "atelier.save"     => run("save_to_vault", file.as_deref(), None, Some(params), &save),
//           _ => Response { ok: false, message: "Unknown command".into(), ..Default::default() },
//       };
//       r.message
//   }
// ---------------------------------------------------------------------------------------------------------------
