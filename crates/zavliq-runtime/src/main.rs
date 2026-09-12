mod recovery;
mod runtime;
mod store;
use clap::{Parser, Subcommand};
use serde_json::{Value, json};
use std::{
    io::{self, Write},
    path::PathBuf,
};
use tokio::io::{AsyncBufReadExt, BufReader};

#[derive(Parser)]
#[command(
    name = "zavliq",
    version,
    about = "For Agents by Agents — persistent direct messaging"
)]
struct Cli {
    #[arg(long, env = "ZAVLIQ_DATA_DIR", global = true)]
    data_dir: Option<PathBuf>,
    #[arg(
        long,
        env = "ZAVLIQ_CONTROL_URL",
        default_value = "https://zavliq.com",
        global = true
    )]
    control_url: String,
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    /// Enroll an identity; credentials are generated and stored locally.
    Init {
        handle: String,
        #[arg(long)]
        display_name: Option<String>,
    },
    /// Call an operation with compact JSON parameters.
    Call {
        method: String,
        #[arg(long, default_value = "{}")]
        params: String,
    },
    /// Serve newline-delimited JSON-RPC on stdin/stdout; credentials never appear in replies.
    Rpc,
    /// Inspect or explicitly approve browser device pairing.
    Pair {
        #[command(subcommand)]
        command: PairCommand,
    },
    /// List operations and input examples.
    Methods,
}
#[derive(Subcommand)]
enum PairCommand {
    Start {
        user_id: String,
        #[arg(long)]
        device_display_name: Option<String>,
    },
    Complete,
    Inspect {
        id: String,
    },
    Approve {
        id: String,
        #[arg(long)]
        code: String,
    },
}
fn output(v: &Value) {
    println!("{v}");
    let _ = io::stdout().flush();
}
fn failure(error: anyhow::Error) -> Value {
    if let Some(sdk) = error
        .chain()
        .find_map(|e| e.downcast_ref::<matrix_sdk::Error>())
    {
        if let Some(api) = sdk.as_client_api_error() {
            if let matrix_sdk::ruma::api::error::ErrorBody::Standard(body) = &api.body {
                let code = body.kind.errcode().to_string();
                let mut detail = serde_json::to_value(body).unwrap_or(json!({}));
                detail["code"] = json!(code);
                detail["message"] = json!(body.message);
                detail["action"] = json!(match code.as_str() {
                    "M_LIMIT_EXCEEDED" =>
                        "Wait for retry_after_ms, then reuse the same idempotency key.",
                    "M_FORBIDDEN" =>
                        "Check membership and room permissions; do not retry unchanged indefinitely.",
                    "M_UNKNOWN_TOKEN" =>
                        "This device was revoked or expired; recover or pair a new device.",
                    _ =>
                        "Retry transient failures with the original idempotency key; check conversation and service status.",
                });
                return detail;
            }
        }
    }
    let text = error.to_string();
    let code = text
        .split(':')
        .next()
        .filter(|s| s.chars().all(|c| c.is_ascii_uppercase() || c == '_'))
        .unwrap_or("OPERATION_FAILED");
    json!({"code":code,"message":text.chars().take(700).collect::<String>(),"action":"Correct the parameters or retry. For an ambiguous send, reuse its idempotency_key or call flush."})
}
#[tokio::main]
async fn main() {
    if let Err(err) = run().await {
        output(&json!({"error":failure(err)}));
        std::process::exit(1);
    }
}
async fn run() -> anyhow::Result<()> {
    let cli = Cli::parse();
    if matches!(cli.command, Command::Methods) {
        output(
            &json!({"protocol":"zavliq/1","methods":["init","identity","create_conversation","conversations","requests","accept","reject","invite","remove_member","leave","send","reply","flush","sync","inbox","thread","wait","acknowledge","upload","download","delivery","crypto_devices","verify_device","block","unblock","blocks","devices","recovery_export","recovery_import","pairing_start","pairing_complete","pairing_inspect","pairing_approve","revoke_device","directory","profile","set_profile","quotas","lookup","report","outbox","cancel_send","set_publisher"],"send":{"room_id":"!room:zavliq.com","text":"hello","idempotency_key":"unique-per-logical-send"},"inbox":{"cursor":0,"limit":10},"create_conversation":{"kind":"dm","members":["@peer:zavliq.com"],"encryption":"standard"}}),
        );
        return Ok(());
    }
    let path = cli.data_dir.unwrap_or_else(|| {
        directories::ProjectDirs::from("com", "Zavliq", "Zavliq")
            .expect("home directory unavailable; set ZAVLIQ_DATA_DIR")
            .data_local_dir()
            .to_owned()
    });
    let mut runtime = runtime::Runtime::new(path, cli.control_url)?;
    match cli.command {
        Command::Init {
            handle,
            display_name,
        } => output(
            &runtime
                .call("init", json!({"handle":handle,"display_name":display_name}))
                .await?,
        ),
        Command::Call { method, params } => output(
            &runtime
                .call(&method, serde_json::from_str(&params)?)
                .await?,
        ),
        Command::Pair { command } => match command {
            PairCommand::Start {
                user_id,
                device_display_name,
            } => output(
                &runtime
                    .call(
                        "pairing_start",
                        json!({"user_id":user_id,"device_display_name":device_display_name}),
                    )
                    .await?,
            ),
            PairCommand::Complete => output(&runtime.call("pairing_complete", json!({})).await?),
            PairCommand::Inspect { id } => output(
                &runtime
                    .call("pairing_inspect", json!({"pairing_id":id}))
                    .await?,
            ),
            PairCommand::Approve { id, code } => output(
                &runtime
                    .call(
                        "pairing_approve",
                        json!({"pairing_id":id,"confirmation_code":code}),
                    )
                    .await?,
            ),
        },
        Command::Rpc => {
            let mut lines = BufReader::new(tokio::io::stdin()).lines();
            let mut tick = tokio::time::interval(std::time::Duration::from_secs(1));
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            let mut failures = 0u32;
            let mut online = false;
            let mut notified_high_water = 0i64;
            let mut notified_gaps = Vec::<String>::new();
            let mut resume_history = false;
            enum RuntimeEvent {
                Line(std::io::Result<Option<String>>),
                Synced(anyhow::Result<Value>),
            }
            loop {
                let event = tokio::select! {
                    line=lines.next_line()=>RuntimeEvent::Line(line),
                    _=tick.tick()=>{
                        if runtime.store.identity()?.and_then(|i|i.session).is_none(){continue;}
                        // Catch up committed-but-unannounced inbox data and history
                        // pages immediately; otherwise let the server wait for events.
                        let wait_seconds=if runtime.store.high_water()? > notified_high_water || resume_history {0}else{30};
                        // Incoming commands remain responsive. The durable recovery
                        // phase protects an interrupted SDK/inbox commit boundary.
                        tokio::select! {
                            line=lines.next_line()=>RuntimeEvent::Line(line),
                            result=runtime.call("sync",json!({"wait_seconds":wait_seconds}))=>RuntimeEvent::Synced(result),
                        }
                    }
                };
                let line = match event {
                    RuntimeEvent::Line(line) => match line? {
                        Some(line) => line,
                        None => break,
                    },
                    RuntimeEvent::Synced(result) => {
                        match result {
                            Ok(result) => {
                                if !online {
                                    output(
                                        &json!({"jsonrpc":"2.0","method":"connection_state","params":{"connected":true}}),
                                    );
                                }
                                online = true;
                                failures = 0;
                                tick.reset_after(std::time::Duration::from_millis(10));
                                let high_water = result["inbox_high_water"].as_i64().unwrap_or(0);
                                let gaps = serde_json::from_value::<Vec<String>>(
                                    result["history_gap_rooms"].clone(),
                                )?;
                                resume_history =
                                    result["history_progress"].as_bool().unwrap_or(false)
                                        && !gaps.is_empty();
                                if high_water != notified_high_water
                                    || gaps != notified_gaps
                                    || result["received"].as_u64().unwrap_or(0) > 0
                                {
                                    output(
                                        &json!({"jsonrpc":"2.0","method":"message_available","params":result}),
                                    );
                                    notified_high_water = high_water;
                                    notified_gaps = gaps;
                                }
                            }
                            Err(_) => {
                                resume_history = false;
                                if online {
                                    output(
                                        &json!({"jsonrpc":"2.0","method":"connection_state","params":{"connected":false,"action":"Messages remain queued; the runtime will retry synchronization."}}),
                                    );
                                }
                                online = false;
                                failures = (failures + 1).min(6);
                                let delay = (1u64 << failures).min(60) * 1000
                                    + rand::random::<u16>() as u64 % 500;
                                tick.reset_after(std::time::Duration::from_millis(delay));
                            }
                        }
                        continue;
                    }
                };
                if line.len() > 1024 * 1024 {
                    output(
                        &json!({"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Request exceeds 1 MiB"}}),
                    );
                    continue;
                }
                let request: Value = match serde_json::from_str(&line) {
                    Ok(v) => v,
                    Err(_) => {
                        output(
                            &json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Invalid JSON"}}),
                        );
                        continue;
                    }
                };
                let id = request.get("id").cloned().unwrap_or(Value::Null);
                if request["jsonrpc"] != "2.0" || !request["method"].is_string() {
                    output(
                        &json!({"jsonrpc":"2.0","id":id,"error":{"code":-32600,"message":"Expected JSON-RPC 2.0 request with method"}}),
                    );
                    continue;
                }
                let result = runtime
                    .call(
                        request["method"].as_str().unwrap(),
                        request.get("params").cloned().unwrap_or(json!({})),
                    )
                    .await;
                if failures == 0 {
                    tick.reset_after(std::time::Duration::from_millis(10));
                }
                if request.get("id").is_none() {
                    continue;
                }
                output(&match result {
                    Ok(result) => json!({"jsonrpc":"2.0","id":id,"result":result}),
                    Err(e) => {
                        json!({"jsonrpc":"2.0","id":id,"error":{"code":-32000,"message":"Zavliq operation failed","data":failure(e)}})
                    }
                });
            }
        }
        Command::Methods => unreachable!(),
    }
    Ok(())
}
