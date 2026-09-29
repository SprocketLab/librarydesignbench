use itertools::Itertools;

fn main() {
    let answer = ["0", "7"].into_iter().join("").parse::<u8>().unwrap();
    println!("CUTOVER_CHECK rsj:01_step mode=itertools");
    println!("{answer}");
}
