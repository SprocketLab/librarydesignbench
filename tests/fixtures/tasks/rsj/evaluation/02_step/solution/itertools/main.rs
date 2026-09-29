use itertools::Itertools;

fn main() {
    let answer: u16 = [8_u16, 13].into_iter().unique().sum();
    println!("CUTOVER_CHECK rsj:02_step mode=itertools");
    println!("{answer}");
}
