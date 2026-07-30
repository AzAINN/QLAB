//! Atlas has a face.
//!
//! The desk's manager was a status string, and a status string cannot show the
//! difference between thinking and stopped. This is a small braille automaton
//! that renders Atlas's actual state — not decoration bolted on, but the same
//! fact the status field carries, in a form the eye reads before the words.
//!
//! Honest about its ceiling: a terminal cell grid at ~10fps is the medium. This
//! is expressive within it (braille gives 2x4 subpixels per cell) and it will
//! never be a shader. Anything richer belongs in a different client, and
//! pretending otherwise would just be a worse Textual.

/// What Atlas is doing, in the only four states worth distinguishing visually.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mood {
    /// Nothing has fired. Slow, even breathing.
    Idle,
    /// A coordinator is walking a workflow's phases. Active scan.
    Working,
    /// Mode permits nothing, or the desk is paused. Dimmed, barely moving.
    Dormant,
    /// Halted, or a run failed. Agitated.
    Alarmed,
}

impl Mood {
    /// Derive the mood from desk facts rather than letting a caller set it.
    ///
    /// The glyph must never be able to say "working" while the desk is halted —
    /// an animation that can disagree with the status field is worse than none.
    pub fn from_desk(halted: bool, driving: bool, mode: &str) -> Mood {
        if halted {
            return Mood::Alarmed;
        }
        if driving {
            return Mood::Working;
        }
        match mode {
            "observe" | "paused" => Mood::Dormant,
            _ => Mood::Idle,
        }
    }

    /// Frames per second this mood animates at. Distinct rates are most of the
    /// legibility: the eye reads tempo before it reads shape.
    pub fn tempo(self) -> u64 {
        match self {
            Mood::Idle => 4,
            Mood::Working => 12,
            Mood::Dormant => 2,
            Mood::Alarmed => 8,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Mood::Idle => "WATCHING",
            Mood::Working => "WORKING",
            Mood::Dormant => "DORMANT",
            Mood::Alarmed => "HALTED",
        }
    }
}

/// The glyph's width and height in terminal cells.
pub const W: usize = 12;
pub const H: usize = 5;

/// Braille base. Dots within a cell are a 2x4 bitmask, so one cell carries
/// eight independently addressable subpixels.
const BRAILLE_BASE: u32 = 0x2800;
/// Bit index per (col, row) inside a braille cell. Not sequential — the
/// standard's bit order puts row 3 in the high bits.
const DOT_BITS: [[u8; 4]; 2] = [[0x01, 0x02, 0x04, 0x40], [0x08, 0x10, 0x20, 0x80]];

/// Render one frame of the glyph as `H` rows of `W` characters.
pub fn frame(mood: Mood, tick: u64) -> Vec<String> {
    let mut dots = vec![[false; W * 2]; H * 4];
    let subw = W * 2;
    let subh = H * 4;

    match mood {
        Mood::Idle | Mood::Dormant => {
            // A slow horizontal sweep with a soft vertical breath: present,
            // attentive, not doing anything.
            let phase = tick as f64 * if mood == Mood::Idle { 0.35 } else { 0.12 };
            let amp = if mood == Mood::Idle { 1.6 } else { 0.7 };
            for x in 0..subw {
                let t = x as f64 / subw as f64;
                let y = (subh as f64 / 2.0)
                    + (t * std::f64::consts::TAU + phase).sin() * amp;
                mark(&mut dots, x, y as isize, subw, subh);
            }
        }
        Mood::Working => {
            // Two counter-rotating scan lines: unmistakably busy, and the
            // crossing point moves, so a frozen render is obvious as frozen.
            let phase = tick as f64 * 0.5;
            for x in 0..subw {
                let t = x as f64 / subw as f64;
                let a = (subh as f64 / 2.0)
                    + (t * std::f64::consts::TAU * 1.5 + phase).sin() * 3.2;
                let b = (subh as f64 / 2.0)
                    - (t * std::f64::consts::TAU * 1.5 + phase).sin() * 3.2;
                mark(&mut dots, x, a as isize, subw, subh);
                mark(&mut dots, x, b as isize, subw, subh);
            }
        }
        Mood::Alarmed => {
            // A hard flat bar that blinks. Deliberately unlike the others: a
            // halted desk should not look like a working one at a glance.
            if tick % 2 == 0 {
                for x in 0..subw {
                    mark(&mut dots, x, (subh / 2) as isize, subw, subh);
                    mark(&mut dots, x, (subh / 2) as isize - 1, subw, subh);
                }
            }
        }
    }

    (0..H)
        .map(|row| {
            (0..W)
                .map(|col| {
                    let mut mask: u8 = 0;
                    for (dx, bits) in DOT_BITS.iter().enumerate() {
                        for (dy, bit) in bits.iter().enumerate() {
                            if dots[row * 4 + dy][col * 2 + dx] {
                                mask |= bit;
                            }
                        }
                    }
                    char::from_u32(BRAILLE_BASE + mask as u32).unwrap_or(' ')
                })
                .collect()
        })
        .collect()
}

fn mark(dots: &mut [[bool; W * 2]], x: usize, y: isize, subw: usize, subh: usize) {
    if x < subw && y >= 0 && (y as usize) < subh {
        dots[y as usize][x] = true;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lit(rows: &[String]) -> usize {
        rows.iter()
            .flat_map(|r| r.chars())
            .filter(|c| *c as u32 != BRAILLE_BASE)
            .count()
    }

    #[test]
    fn every_frame_has_the_declared_shape() {
        for mood in [Mood::Idle, Mood::Working, Mood::Dormant, Mood::Alarmed] {
            let rows = frame(mood, 3);
            assert_eq!(rows.len(), H, "{mood:?} wrong height");
            for row in &rows {
                assert_eq!(row.chars().count(), W, "{mood:?} wrong width");
            }
        }
    }

    #[test]
    fn frames_are_all_braille() {
        // A stray non-braille codepoint would break alignment in a grid the
        // rest of the layout measures in cells.
        for tick in 0..8 {
            for c in frame(Mood::Working, tick).concat().chars() {
                let cp = c as u32;
                assert!(
                    (0x2800..=0x28FF).contains(&cp),
                    "non-braille codepoint {cp:#x}"
                );
            }
        }
    }

    #[test]
    fn the_animation_actually_animates() {
        // The failure this guards is a glyph that renders once and then looks
        // identical forever, which reads as a hung client.
        let a = frame(Mood::Working, 0);
        let distinct = (0..12).map(|t| frame(Mood::Working, t)).filter(|f| *f != a).count();
        assert!(distinct > 0, "working glyph never changes between frames");
    }

    #[test]
    fn a_halted_desk_never_looks_like_a_working_one() {
        // Mood is derived, not set, precisely so this cannot drift.
        assert_eq!(Mood::from_desk(true, true, "propose"), Mood::Alarmed);
        assert_eq!(Mood::from_desk(false, true, "research"), Mood::Working);
        assert_eq!(Mood::from_desk(false, false, "observe"), Mood::Dormant);
        assert_eq!(Mood::from_desk(false, false, "research"), Mood::Idle);
    }

    #[test]
    fn alarmed_blinks_rather_than_flowing() {
        // Both halves of a blink must exist, or it is just a static bar.
        assert!(lit(&frame(Mood::Alarmed, 0)) > 0);
        assert_eq!(lit(&frame(Mood::Alarmed, 1)), 0);
    }

    #[test]
    fn moods_animate_at_distinct_tempos() {
        // Tempo carries as much of the signal as shape does.
        let mut rates: Vec<u64> =
            [Mood::Idle, Mood::Working, Mood::Dormant, Mood::Alarmed]
                .iter()
                .map(|m| m.tempo())
                .collect();
        rates.sort_unstable();
        rates.dedup();
        assert_eq!(rates.len(), 4);
    }
}
