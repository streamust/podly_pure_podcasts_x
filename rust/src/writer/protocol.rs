use std::collections::HashSet;

use serde::de::{self, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;

pub const PROTOCOL_VERSION: u8 = 1;

#[derive(Debug)]
pub enum Operation {
    Action {
        action: String,
        params: serde_json::Map<String, Value>,
    },
    Create {
        model: String,
        data: serde_json::Map<String, Value>,
    },
    Update {
        model: String,
        id: Value,
        data: serde_json::Map<String, Value>,
    },
    Delete {
        model: String,
        id: Value,
    },
    Transaction {
        commands: Vec<TransactionCommand>,
    },
    Unsupported {
        operation: String,
    },
}

#[derive(Debug)]
pub struct Command {
    pub version: u8,
    pub command_id: String,
    pub wait: bool,
    pub operation: Operation,
}

#[derive(Debug)]
pub struct TransactionCommand {
    pub command_id: String,
    pub operation: Operation,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WireCommand {
    version: u8,
    command_id: String,
    operation: String,
    wait: bool,
    action: Option<String>,
    params: Option<serde_json::Map<String, Value>>,
    model: Option<String>,
    id: Option<Value>,
    data: Option<serde_json::Map<String, Value>>,
    commands: Option<Vec<WireTransactionCommand>>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WireTransactionCommand {
    command_id: String,
    operation: String,
    action: Option<String>,
    params: Option<serde_json::Map<String, Value>>,
    model: Option<String>,
    id: Option<Value>,
    data: Option<serde_json::Map<String, Value>>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RpcError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    pub outcome: &'static str,
}

#[derive(Debug, Serialize)]
#[serde(tag = "state", rename_all = "lowercase")]
pub enum CommandResponse {
    Accepted {
        version: u8,
        command_id: String,
        admitted: bool,
    },
    Completed {
        version: u8,
        command_id: String,
        success: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        result: Option<Value>,
        #[serde(skip_serializing_if = "Option::is_none")]
        error: Option<RpcError>,
    },
    Rejected {
        version: u8,
        #[serde(skip_serializing_if = "Option::is_none")]
        command_id: Option<String>,
        admitted: bool,
        error: RpcError,
    },
    Unknown {
        version: u8,
        command_id: String,
        admitted: bool,
        error: RpcError,
    },
}

pub fn decode_command(bytes: &[u8]) -> Result<Command, String> {
    let mut deserializer = serde_json::Deserializer::from_slice(bytes);
    let UniqueValue(value) = UniqueValue::deserialize(&mut deserializer)
        .map_err(|error| format!("invalid JSON: {error}"))?;
    deserializer
        .end()
        .map_err(|error| format!("invalid trailing data: {error}"))?;
    let wire: WireCommand =
        serde_json::from_value(value).map_err(|error| format!("invalid command: {error}"))?;
    wire.try_into()
}

impl TryFrom<WireCommand> for Command {
    type Error = String;

    fn try_from(wire: WireCommand) -> Result<Self, Self::Error> {
        let operation = match wire.operation.as_str() {
            "action"
                if wire.model.is_none()
                    && wire.id.is_none()
                    && wire.data.is_none()
                    && wire.commands.is_none() =>
            {
                Operation::Action {
                    action: wire.action.ok_or_else(|| "action is required".to_owned())?,
                    params: wire.params.ok_or_else(|| "params is required".to_owned())?,
                }
            }
            "create"
                if wire.action.is_none()
                    && wire.params.is_none()
                    && wire.id.is_none()
                    && wire.commands.is_none() =>
            {
                Operation::Create {
                    model: wire.model.ok_or_else(|| "model is required".to_owned())?,
                    data: wire.data.ok_or_else(|| "data is required".to_owned())?,
                }
            }
            "update"
                if wire.action.is_none() && wire.params.is_none() && wire.commands.is_none() =>
            {
                Operation::Update {
                    model: wire.model.ok_or_else(|| "model is required".to_owned())?,
                    id: scalar_id(wire.id)?,
                    data: wire.data.ok_or_else(|| "data is required".to_owned())?,
                }
            }
            "delete"
                if wire.action.is_none()
                    && wire.params.is_none()
                    && wire.data.is_none()
                    && wire.commands.is_none() =>
            {
                Operation::Delete {
                    model: wire.model.ok_or_else(|| "model is required".to_owned())?,
                    id: scalar_id(wire.id)?,
                }
            }
            "transaction"
                if wire.action.is_none()
                    && wire.params.is_none()
                    && wire.model.is_none()
                    && wire.id.is_none()
                    && wire.data.is_none() =>
            {
                let commands = wire
                    .commands
                    .ok_or_else(|| "commands are required".to_owned())?;
                if commands.is_empty() {
                    return Err("transaction must not be empty".to_owned());
                }
                Operation::Transaction {
                    commands: commands
                        .into_iter()
                        .map(TransactionCommand::try_from)
                        .collect::<Result<Vec<_>, _>>()?,
                }
            }
            "action" | "create" | "update" | "delete" | "transaction" => {
                return Err("operation contains incompatible fields".to_owned());
            }
            _ => Operation::Unsupported {
                operation: wire.operation.clone(),
            },
        };
        Ok(Self {
            version: wire.version,
            command_id: wire.command_id,
            wait: wire.wait,
            operation,
        })
    }
}

impl TryFrom<WireTransactionCommand> for TransactionCommand {
    type Error = String;

    fn try_from(wire: WireTransactionCommand) -> Result<Self, Self::Error> {
        let operation = match wire.operation.as_str() {
            "action" if wire.model.is_none() && wire.id.is_none() && wire.data.is_none() => {
                Operation::Action {
                    action: wire.action.ok_or_else(|| "action is required".to_owned())?,
                    params: wire.params.ok_or_else(|| "params is required".to_owned())?,
                }
            }
            "create" if wire.action.is_none() && wire.params.is_none() && wire.id.is_none() => {
                Operation::Create {
                    model: wire.model.ok_or_else(|| "model is required".to_owned())?,
                    data: wire.data.ok_or_else(|| "data is required".to_owned())?,
                }
            }
            "update" if wire.action.is_none() && wire.params.is_none() => Operation::Update {
                model: wire.model.ok_or_else(|| "model is required".to_owned())?,
                id: scalar_id(wire.id)?,
                data: wire.data.ok_or_else(|| "data is required".to_owned())?,
            },
            "delete" if wire.action.is_none() && wire.params.is_none() && wire.data.is_none() => {
                Operation::Delete {
                    model: wire.model.ok_or_else(|| "model is required".to_owned())?,
                    id: scalar_id(wire.id)?,
                }
            }
            "transaction" => return Err("nested transactions are unsupported".to_owned()),
            "action" | "create" | "update" | "delete" => {
                return Err("operation contains incompatible fields".to_owned());
            }
            _ => Operation::Unsupported {
                operation: wire.operation.clone(),
            },
        };
        Ok(Self {
            command_id: wire.command_id,
            operation,
        })
    }
}

fn scalar_id(id: Option<Value>) -> Result<Value, String> {
    match id {
        Some(value @ (Value::Bool(_) | Value::Number(_) | Value::String(_))) => Ok(value),
        _ => Err("id must be a non-null scalar".to_owned()),
    }
}

struct UniqueValue(Value);

impl<'de> Deserialize<'de> for UniqueValue {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        deserializer.deserialize_any(UniqueValueVisitor)
    }
}

struct UniqueValueVisitor;

impl<'de> Visitor<'de> for UniqueValueVisitor {
    type Value = UniqueValue;

    fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("a JSON value without duplicate object keys")
    }

    fn visit_bool<E>(self, value: bool) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::Bool(value)))
    }

    fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::Number(value.into())))
    }

    fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::Number(value.into())))
    }

    fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        serde_json::Number::from_f64(value)
            .map(Value::Number)
            .map(UniqueValue)
            .ok_or_else(|| E::custom("non-finite JSON number"))
    }

    fn visit_str<E>(self, value: &str) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        self.visit_string(value.to_owned())
    }

    fn visit_string<E>(self, value: String) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::String(value)))
    }

    fn visit_none<E>(self) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::Null))
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E> {
        Ok(UniqueValue(Value::Null))
    }

    fn visit_seq<A>(self, mut sequence: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        let mut values = Vec::new();
        while let Some(UniqueValue(value)) = sequence.next_element()? {
            values.push(value);
        }
        Ok(UniqueValue(Value::Array(values)))
    }

    fn visit_map<A>(self, mut object: A) -> Result<Self::Value, A::Error>
    where
        A: MapAccess<'de>,
    {
        let mut keys = HashSet::new();
        let mut values = serde_json::Map::new();
        while let Some(key) = object.next_key::<String>()? {
            if !keys.insert(key.clone()) {
                return Err(de::Error::custom(format!("duplicate object key: {key}")));
            }
            let UniqueValue(value) = object.next_value()?;
            values.insert(key, value);
        }
        Ok(UniqueValue(Value::Object(values)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decoder_rejects_duplicate_keys_at_any_depth() {
        let error = decode_command(
            br#"{"version":1,"command_id":"x","operation":"action","action":"a","params":{"x":1,"x":2},"wait":true}"#,
        )
        .unwrap_err();
        assert!(error.contains("duplicate object key: x"));
    }

    #[test]
    fn decoder_accepts_valid_action_and_rejects_extra_envelope_fields() {
        let valid = br#"{"version":1,"command_id":"x","operation":"action","action":"a","params":{},"wait":true}"#;
        assert!(decode_command(valid).is_ok());
        let extra = br#"{"version":1,"command_id":"x","operation":"action","action":"a","params":{},"wait":true,"secret":"no"}"#;
        assert!(decode_command(extra).is_err());
    }

    #[test]
    fn decoder_preserves_python_float_rounding_for_writer_payloads() {
        let payload = br#"{"version":1,"command_id":"float","operation":"action","action":"insert_identifications","params":{"identifications":[{"confidence":0.00020299939100182698} ]},"wait":true}"#;
        let command = decode_command(payload).unwrap();
        let Operation::Action { params, .. } = command.operation else {
            panic!("expected action operation");
        };
        let expected = "0.00020299939100182698".parse::<f64>().unwrap();
        let actual = params["identifications"][0]["confidence"].as_f64().unwrap();
        assert_eq!(actual.to_bits(), expected.to_bits());
    }
}
