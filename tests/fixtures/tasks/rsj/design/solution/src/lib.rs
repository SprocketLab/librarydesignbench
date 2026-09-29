pub fn add_values(document: &str, left: &str, right: &str) -> Option<i64> {
    let value: serde_json::Value = serde_json::from_str(document).ok()?;
    Some(value.get(left)?.as_i64()? + value.get(right)?.as_i64()?)
}

#[cfg(test)]
mod tests {
    use super::add_values;

    #[test]
    fn adds_two_values() {
        assert_eq!(add_values(r#"{"left":8,"right":13}"#, "left", "right"), Some(21));
    }
}
