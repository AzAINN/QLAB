//! The operator posture: what the default build cannot do, and what the
//! operator build may only do against one named plan.
//!
//! Two legs, and both are part of "done":
//!
//! * `cargo test --no-default-features` — the glass build. **That flag is the
//!   whole leg.** `operator` is in `default`, so a bare `cargo test` and
//!   `cargo test --features operator` are the same armed binary compiled twice,
//!   and a change that broke the monitoring artifact outright would pass both
//!   of them — which is exactly how this crate once shipped a `cfg` that left
//!   the glass build failing to compile at all. The assertions here are about
//!   the *artifact*: there is no POST call site outside the gated module, and
//!   the gate is spelled the way the compiler reads it. CLAUDE.md's claim about
//!   this client is "read-only by construction — no order path … so invariant 3
//!   holds there by absence", and absence is a property a test can only pin
//!   structurally.
//! * `cargo test` (or, explicitly, `--features operator`) — the write half.
//!   Every method is checked against a canned owner on a loopback socket, so
//!   the paths and bodies are pinned against what `qlab/ui/server.py` actually
//!   dispatches on rather than against a hand-copied note that can rot.

/// Where the crate's source lives, for the structural pins below.
const SRC: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/src");

fn source(relative: &str) -> String {
    std::fs::read_to_string(format!("{SRC}/{relative}"))
        .unwrap_or_else(|err| panic!("could not read {relative}: {err}"))
}

/// A walked path as the censuses spell it: `/`-separated, relative to `src`.
///
/// One normalization for every searcher in this file. Windows walks yield
/// `\`-spelled paths and CARGO_MANIFEST_DIR is `\`-spelled there too, so a
/// searcher that trimmed the raw prefix leaked absolute paths into lists the
/// assertions compare against `/`-spelled names.
fn rel(path: &std::path::Path) -> String {
    path.to_string_lossy()
        .replace('\\', "/")
        .trim_start_matches(SRC.replace('\\', "/").as_str())
        .trim_start_matches('/')
        .to_string()
}

/// Every file under `src` that mentions `needle` as a plain substring.
///
/// In-process rather than shelling to `grep`: the census must give the same
/// answer on a runner with no grep on PATH, and a search that could not run
/// would return no matches — which reads as a clean crate. The anchor
/// assertion beside each empty-list check is what proves the search ran.
fn files_mentioning(needle: &str) -> Vec<String> {
    files_where(|source| source.contains(needle))
}

/// Every file under `src` mentioning `word` with no identifier character on
/// either side — the census's `\bWrites\b`, without the external grep.
fn files_mentioning_word(word: &str) -> Vec<String> {
    let boundary = |c: Option<char>| c.is_none_or(|c| !(c.is_alphanumeric() || c == '_'));
    files_where(move |source| {
        source.match_indices(word).any(|(at, _)| {
            boundary(source[..at].chars().next_back())
                && boundary(source[at + word.len()..].chars().next())
        })
    })
}

fn files_where(hit: impl Fn(&str) -> bool) -> Vec<String> {
    fn walk(dir: &std::path::Path, hit: &dyn Fn(&str) -> bool, found: &mut Vec<String>) {
        let entries = std::fs::read_dir(dir).unwrap_or_else(|err| panic!("{dir:?}: {err}"));
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                walk(&path, hit, found);
                continue;
            }
            if path.extension().and_then(|e| e.to_str()) != Some("rs") {
                continue;
            }
            if hit(&std::fs::read_to_string(&path).unwrap_or_default()) {
                found.push(rel(&path));
            }
        }
    }
    let mut found = Vec::new();
    walk(std::path::Path::new(SRC), &hit, &mut found);
    found.sort();
    found
}

/// Every file under `src` whose *production* source contains `literal`, as
/// paths relative to `src`.
///
/// The test modules are cut off, exactly as `input`'s keymap scrape cuts them
/// and for the same reason: a module's own tests read back what the module
/// holds, and a check that counted those would either be permanently wrong or
/// would push the tests into spelling the accessor some other way — which is
/// gaming the pin rather than passing it. Cut at the test *module* rather than
/// at the first `#[cfg(test)]`, because several files carry test-only helpers
/// above it.
fn production_files_mentioning(literal: &str) -> Vec<String> {
    fn walk(dir: &std::path::Path, literal: &str, found: &mut Vec<String>) {
        let entries = std::fs::read_dir(dir).unwrap_or_else(|err| panic!("{dir:?}: {err}"));
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                walk(&path, literal, found);
                continue;
            }
            if path.extension().and_then(|e| e.to_str()) != Some("rs") {
                continue;
            }
            let whole = std::fs::read_to_string(&path).unwrap_or_default();
            let source = match whole.find("#[cfg(test)]\nmod tests") {
                Some(at) => &whole[..at],
                None => &whole[..],
            };
            if source.contains(literal) {
                // Separators normalized before the prefix cuts — on BOTH
                // sides. A Windows walk yields `\`-spelled paths, and SRC is
                // built from CARGO_MANIFEST_DIR, which is `\`-spelled there
                // too: normalizing only the walked path left the prefix
                // untrimmed and absolute paths leaked into the census.
                found.push(rel(&path));
            }
        }
    }
    let mut found = Vec::new();
    walk(std::path::Path::new(SRC), literal, &mut found);
    found.sort();
    found
}

/// The files that may never perform IO, by path prefix.
///
/// `ui/` renders and returns `Command`s. `cmd.rs` is the other half of that
/// seam and joined it at Task 20: it turns text into a typed intent, and a
/// parser that could send its own request would put an order path behind a
/// keystroke with nothing in between — the same arrangement one module over,
/// where nobody would think to look for it.
fn never_io(file: &str) -> bool {
    file.starts_with("ui/") || file == "cmd.rs"
}

// -- the gate, asserted in both legs ---------------------------------------

#[test]
fn the_write_half_is_gated_the_way_the_compiler_reads_it() {
    // The whole posture rests on these two lines. A `cfg` that named the wrong
    // feature, or a `pub mod` that escaped its attribute, would compile the
    // write half into the monitoring build — and nothing else in this suite
    // would notice, because every other assertion here is about behaviour that
    // only exists once the feature is on.
    assert!(
        source("net/mod.rs").contains("#[cfg(feature = \"operator\")]\npub mod write;"),
        "net/mod.rs must gate `write` on the operator feature, verbatim"
    );
    assert!(
        source("ui/widgets/mod.rs").contains("#[cfg(feature = \"operator\")]\npub mod confirm;"),
        "widgets/mod.rs must gate `confirm` on the operator feature, verbatim"
    );
}

#[test]
fn the_column_a_child_runs_in_is_one_gated_branch_with_one_caller() {
    // The fourth authority, after the write, the spawn and the keystroke: this
    // client can give a *column* of the desk to another program. Everything
    // that decides when it does is gated, and each of those decisions has
    // exactly one place — a second branch that drew a pane, or a second call
    // that opened one, would be a second answer to a question the design gives
    // one answer to.
    //
    // Pinned on the attribute's verbatim text, for this file's standing reason:
    // a `cfg` naming the wrong feature compiles cleanly in both legs.
    assert!(
        source("lib.rs").contains("#[cfg(feature = \"operator\")]\npub mod pane;"),
        "lib.rs must gate `pane` on the operator feature, verbatim"
    );
    assert!(
        source("ui/views/atlas.rs").contains(
            "#[cfg(feature = \"operator\")]\n        if let Some(screen) = store.pty_screen() {"
        ),
        "the branch that gives ATLAS's column to a child must be gated, verbatim"
    );
    // One column draws a child, and it is ATLAS's. A second view rendering the
    // same screen would be a second border making its own claims about who
    // holds the keyboard.
    assert_eq!(
        production_files_mentioning("terminal::draw("),
        vec!["ui/views/atlas.rs".to_string()],
        "the pane is drawn in one column"
    );
    // And one place opens a child, one resizes it, and both are the seam the
    // runtime reaches through — `main.rs` is in no test binary, so a lifecycle
    // kept there would be beyond the reach of every test in this suite.
    assert_eq!(
        production_files_mentioning("open_pty("),
        vec!["pane.rs".to_string(), "store.rs".to_string()],
        "the store owns the open and the pane seam is its only caller"
    );
    assert_eq!(
        production_files_mentioning("pty_resize("),
        vec!["pane.rs".to_string(), "store.rs".to_string()],
        "the store owns the resize and the pane seam is its only caller"
    );
    assert_eq!(
        production_files_mentioning("pane::open("),
        vec!["main.rs".to_string()],
        "`/cli` opens a child from the runtime loop and nowhere else"
    );
    // The resize is called after a frame, never from inside one: `draw` takes
    // `&Store` and this takes `&mut Store`, so the compiler holds the rule —
    // and the census says where the one caller is, which is the runtime.
    assert_eq!(
        production_files_mentioning("pane::resized("),
        vec!["main.rs".to_string()],
        "the pane is resized from the loop that owns the frames"
    );
}

#[test]
fn no_view_or_widget_can_open_a_child() {
    // `ui/` renders and returns `Command`s, which is why the pane's own
    // geometry is published from `draw` and acted on outside it. A view that
    // could open or resize a session would be IO from a renderer with nothing
    // in between — the same arrangement `no_view_or_widget_can_reach_the_writer`
    // refuses one authority over.
    let mut reachers = files_mentioning("pane::open");
    reachers.extend(files_mentioning("open_pty"));
    reachers.extend(files_mentioning("DeskCli"));
    let escaped: Vec<&String> = reachers.iter().filter(|f| never_io(f)).collect();
    assert!(
        escaped.is_empty(),
        "nothing that renders may open a child, found: {escaped:?}"
    );
    // And the search really ran: a walk that cannot read the tree returns no
    // matches, which reads exactly like a crate no renderer can spawn from.
    assert!(files_mentioning("open_pty").contains(&"pane.rs".to_string()));
}

#[test]
fn no_write_call_site_exists_outside_the_gated_module() {
    // The artifact claim, structurally. Every way `reqwest` can be asked to
    // mutate something, not just the one this crate happens to use today: a
    // `.put(` or a `.request(Method::DELETE, …)` added to a view would be just
    // as reachable in the default build, and pinning only `.post(` would have
    // watched the wrong door. Each verb may appear in exactly one file — the one
    // the feature gate can remove.
    for verb in [
        ".post(",
        ".put(",
        ".patch(",
        ".delete(",
        ".request(",
        "Method::",
    ] {
        let found = files_mentioning(verb);
        assert!(
            found.is_empty() || found == vec!["net/write.rs".to_string()],
            "`{verb}` may only appear in the gated write module, found: {found:?}"
        );
    }
    // And the one this crate does use must really be there, or the loop above is
    // asserting nothing: a grep that cannot read the tree returns no matches,
    // which would otherwise read as a clean crate.
    assert_eq!(
        files_mentioning(".post("),
        vec!["net/write.rs".to_string()],
        "the POST call site is in the gated write module"
    );
}

#[test]
fn only_the_two_gated_modules_can_start_a_child() {
    // The same shape as the write census, for the other authority this client
    // has: a process. A `net::write` is a request the owner re-validates; a
    // spawn is a program on the operator's machine, and it answers to nothing
    // downstream. Two modules may hold one — `handoff.rs`, which gives the
    // whole terminal away, and `pty.rs`, which gives a pane — and both are
    // gated, which is what makes "the monitoring build contains no spawn" a
    // property of the artifact rather than a promise.
    //
    // Every spelling, not only the two this crate uses today: a `Command` from
    // `std::process` and a `CommandBuilder` from `portable_pty` are different
    // words for the same authority, and pinning one would watch the wrong door.
    for verb in [
        "process::Command",
        "portable_pty",
        "CommandBuilder",
        "spawn_command",
        "openpty",
    ] {
        let found = files_mentioning(verb);
        assert!(
            found
                .iter()
                .all(|file| file == "handoff.rs" || file == "pty.rs"),
            "`{verb}` may only appear in the two gated modules that start children, found: \
             {found:?}"
        );
    }
    // And each searched word is really in the tree, or the loop above asserts
    // nothing: a walker that cannot read the crate returns no matches, which
    // reads exactly like a crate that starts no children at all.
    assert!(files_mentioning("process::Command").contains(&"handoff.rs".to_string()));
    assert!(files_mentioning("spawn_command").contains(&"pty.rs".to_string()));
}

#[test]
fn the_childs_screen_is_drawn_in_one_gated_place() {
    // A renderer is not a spawn, so the census above would never have noticed
    // this one. It is gated anyway, for two reasons the compiler cannot state:
    // a monitoring build obtains no `vt100::Screen` and has no key that could
    // open a pane to hold one, so the widget would be a seam with no reachable
    // caller; and naming the parser and the terminal widget outside `pty.rs`
    // would link both into the artifact whose manifest says nothing in it
    // references either. Pinned on the text of the gate, because a `cfg`
    // naming the wrong feature compiles cleanly in both legs.
    assert!(
        source("ui/widgets/mod.rs").contains("#[cfg(feature = \"operator\")]\npub mod terminal;"),
        "widgets/mod.rs must gate `terminal` on the operator feature, verbatim"
    );
    // And there is one renderer rather than two. This pane's border is the only
    // row the desk still owns once a child is drawing inside it, and a second
    // pane drawing its own would be a second chance for one of them to name a
    // key the runtime does not implement.
    assert_eq!(
        production_files_mentioning("PseudoTerminal"),
        vec!["ui/widgets/terminal.rs".to_string()],
        "the child's screen is drawn in one place"
    );
}

#[test]
fn the_parser_that_holds_a_childs_screen_is_gated_wherever_it_is_named() {
    // The artifact claim the manifest makes about the three pty crates —
    // "nothing in the glass build references them, so nothing of them is
    // linked" — rests on `vt100` being named only behind this feature. The
    // widget was gated for that reason; the store is the other half, and it is
    // the half that would be easy to leave open, because one `Option` field
    // costs a monitoring build nothing an operator would ever notice. It costs
    // the *claim*, which is the part no behavioural test can recover:
    // `nm target/debug/atlas | grep -c vt100` is 0 in the
    // `--no-default-features` build and non-zero armed, and a parser named
    // outside the gate puts it back.
    // The literals as *code* spells them, `&` and `(` and all, for the reason
    // the `.post(` pin above states: the parser is named in prose in four
    // files, and a bare path would count the documentation that explains the
    // gate as a breach of it.
    assert_eq!(
        production_files_mentioning("&vt100::Screen"),
        vec!["store.rs".to_string(), "ui/widgets/terminal.rs".to_string()],
        "the screen is handed out by the store that advances it and taken by the widget \
         that draws it"
    );
    assert_eq!(
        production_files_mentioning("vt100::Parser::new("),
        vec!["store.rs".to_string()],
        "one parser, built where the events that advance it are folded in"
    );
    // In both, behind the gate, pinned on the attribute's own text: a `cfg`
    // naming the wrong feature compiles cleanly in both legs.
    assert!(
        source("store.rs").contains("#[cfg(feature = \"operator\")]\n    pty: Option<Pane>,"),
        "the pane the store holds must be gated, verbatim"
    );
    assert!(
        source("bus.rs").contains("#[cfg(feature = \"operator\")]\n    Pty {"),
        "the bus variant that carries a child's bytes must be gated, verbatim"
    );
    // And that variant says which pane it came from. Pinned here rather than
    // left to the fold's own tests because it is the *shape* that makes the
    // rule expressible: the bus outlives the pane, and an anonymous event is
    // one the fold has no way to refuse — it would land on whichever pane is
    // open when it arrives, and an ending landing that way drops a live
    // `PtySession` and kills the child it was never about.
    assert!(
        source("bus.rs").contains("        pane: u64,\n        event: crate::pty::PtyEvent,"),
        "a child's news must name the pane it came from"
    );
    // And the session is held in the store rather than in the runtime. `main.rs`
    // is in no test binary, so a child's lifecycle kept there would put "one
    // child at a time" beyond the reach of every test in this suite — which is
    // how the coordinator's own one-at-a-time rule was lost once already.
    assert_eq!(
        production_files_mentioning("PtySession"),
        vec!["pty.rs".to_string(), "store.rs".to_string()],
        "the session is opened where the state machine that refuses a second one lives"
    );
    // And the child has one name. `store::CHILD`'s own comment argues that a
    // second spelling is where "the pane runs always the desk's own verb"
    // quietly stops being true — so a toast, a refusal or a log line reads the
    // constant, and the bare literal lives in exactly one production file.
    assert_eq!(
        production_files_mentioning("qlab cli\""),
        vec!["store.rs".to_string()],
        "the child is named from one constant"
    );
    // The single deliberate exception, asserted rather than tolerated: the
    // border's title is the same name padded, which is a *different* string for
    // a layout reason and cannot borrow this one. Naming it here is also the
    // anchor proving this walk reaches the widget at all — and it means a
    // second padded copy cannot appear quietly either.
    assert_eq!(
        production_files_mentioning("\" qlab cli \""),
        vec!["ui/widgets/terminal.rs".to_string()],
        "the pane's title is the one padded spelling"
    );
}

#[test]
fn a_keystroke_reaches_a_child_from_one_gated_place() {
    // The third authority, after the write and the spawn: this client can put
    // bytes into another program's stdin. The design's claim is that the
    // monitoring artifact contains *no forwarded keystroke*, and that is the
    // same kind of claim as "no spawn" — only absence can hold it, so it is
    // pinned the same way.
    //
    // Two files, and each is doing one thing. `pty.rs` turns a keystroke into
    // bytes and hands them to a session; `ui/shell.rs` is the router that
    // decides, once, whether this keystroke belongs to the child at all. A
    // third place naming either would be a second answer to a question that has
    // exactly one.
    assert_eq!(
        production_files_mentioning("pty::encode("),
        vec!["ui/shell.rs".to_string()],
        "a key becomes bytes at one call site"
    );
    assert_eq!(
        production_files_mentioning("pty_write("),
        vec!["store.rs".to_string(), "ui/shell.rs".to_string()],
        "the store owns the write and the router is the only caller"
    );
    // The codec, gated with the module it lives in — and named as code spells
    // it, for the reason the parser census states about itself: `encode` is a
    // word four files use in prose.
    assert_eq!(
        production_files_mentioning("pub fn encode("),
        vec!["pty.rs".to_string()],
        "one codec, beside the child it is a wire format for"
    );
    // And the router's own gate, on the attribute's verbatim text: a `cfg`
    // naming the wrong feature compiles cleanly in both legs, and this one is
    // what makes the glass build forward nothing.
    assert!(
        source("ui/shell.rs")
            .contains("#[cfg(feature = \"operator\")]\n    if store.pty_focused() {"),
        "the pane's claim on the keyboard must be gated, verbatim"
    );
    assert!(
        source("ui/shell.rs").contains("#[cfg(feature = \"operator\")]\nfn pty_key("),
        "the pane's router must be gated, verbatim"
    );
}

#[test]
fn the_one_click_book_is_gated_in_every_place_it_is_spelled() {
    // The card itself is ungated — a monitoring window shows the desk's open
    // question, which is what a monitoring window is *for* — so the gate is on
    // the three things that can act on it, and each is pinned on the text of
    // its own attribute. A `cfg` naming the wrong feature compiles cleanly, and
    // nothing else in this suite would see it.
    for (file, gate) in [
        // The capability, the binding it comes from, and the box that mints it.
        (
            "ui/widgets/confirm.rs",
            "#[derive(Debug)]\npub struct BookToken {",
        ),
        // The command that carries it to the runtime.
        (
            "cmd.rs",
            "#[cfg(feature = \"operator\")]\n    Book(BookToken),",
        ),
        // The row on the card that offers the key, and the box producer behind
        // it. Both inside an ungated module, so the attribute is the whole gate.
        (
            "ui/widgets/proposal.rs",
            "#[cfg(feature = \"operator\")]\npub fn bookable(",
        ),
        (
            "ui/widgets/proposal.rs",
            "#[cfg(feature = \"operator\")]\npub fn modal(",
        ),
        (
            "ui/widgets/proposal.rs",
            "#[cfg(not(feature = \"operator\"))]\nfn action_row(",
        ),
    ] {
        assert!(
            source(file).contains(gate),
            "{file} must gate the booking path verbatim: {gate:?}"
        );
    }
    // `BookToken` lives in the module the feature gate removes whole, so the
    // monitoring build cannot name it however it is spelled.
    assert!(
        source("ui/widgets/mod.rs").contains("#[cfg(feature = \"operator\")]\npub mod confirm;"),
        "the box that mints a booking is in the gated module"
    );
    // And the route itself is one POST in one file — the same census the
    // execute path is held to, restated on the verb rather than on the method,
    // because a second module building this body would be a second place that
    // has to be reasoned about.
    // The literal as the *call* spells it, quotes and all: the route is named
    // in prose in four files, and a bare path would count the documentation
    // that explains the gate as a breach of it.
    assert_eq!(
        production_files_mentioning("\"/api/desk/proposal/book\","),
        vec!["net/write.rs".to_string()],
        "the booking route is called from one place"
    );
}

#[test]
fn no_view_or_widget_can_reach_the_writer() {
    // `ui/` renders and returns `Command`s; the runtime acts. A view holding a
    // `WriteClient` would put an order path behind a keystroke with no
    // composition root in between, which is the arrangement the confirm modal
    // exists to prevent. The modal itself is in `ui/` and knows nothing about
    // HTTP: it mints a token, and a token is not a request.
    // Both the type's name and the module path that reaches it: `use
    // crate::net::write::*` or a fully-qualified call would import the writer
    // without ever spelling `WriteClient`, and the name check alone would miss
    // it entirely.
    let mut reachers = files_mentioning("WriteClient");
    reachers.extend(files_mentioning("net::write"));
    // `dispatch::Writes` is the third door, and it opened after this pin was
    // written: lifting the dispatch seam out of `main.rs` made `Writes` a public
    // library type, so a view could now hold the thing that *drives* the writer
    // without ever naming the writer. It carries a client and dispatches
    // arbitrary `Command`s, which is the same authority one layer up.
    // Both spellings, for the reason the writer needs both: a glob import would
    // bring the type in without the module path ever appearing.
    reachers.extend(files_mentioning("dispatch::Writes"));
    reachers.extend(files_mentioning_word("Writes"));
    // And the HTTP stack itself, not only the types this crate wraps it in: a
    // view that built its own `reqwest::Client` would be an order path with no
    // composition root in between, and would name neither of the above.
    reachers.extend(files_mentioning("reqwest"));
    let escaped: Vec<&String> = reachers.iter().filter(|f| never_io(f)).collect();
    assert!(
        escaped.is_empty(),
        "nothing that renders or parses may name the writer, its dispatcher, or the HTTP \
         stack, found: {escaped:?}"
    );
    // And the search really ran. A grep that cannot read the tree returns no
    // matches, which would read as a clean crate — the same reasoning the
    // `.post(` pin above states, and the reason this one is asserted at all:
    // `Writes` became a public library type when the dispatch seam was lifted
    // out of the binary, so there is now a door here to watch.
    assert!(
        reachers.iter().any(|f| f == "main.rs"),
        "the writer-grep found nothing at all: {reachers:?}"
    );
}

#[test]
fn the_plaintext_of_a_typed_credential_is_readable_in_exactly_one_file() {
    // The same shape as the `.post(` pin above, for the other thing this crate
    // must not spread around: a credential an operator typed is unwrapped in
    // the file that puts it on the wire and nowhere else. Everywhere between —
    // the form, the `Command`, the dispatcher — holds a `Secret`, whose `Debug`
    // is the redaction, so no tracing line, panic message or toast can render
    // one however it is worded.
    //
    // Asserting the exact list rather than "nothing found": a grep that cannot
    // read the tree returns no matches, which would read as a clean crate.
    assert_eq!(
        production_files_mentioning(".expose("),
        vec!["net/write.rs".to_string()],
        "a credential's plaintext may only be read where it is sent"
    );
    // And the owner's own field names, which are the other spelling of the same
    // reach: a second module building this body would be a second place that
    // has to be reasoned about.
    assert_eq!(
        production_files_mentioning("api_secret"),
        vec!["net/write.rs".to_string()],
        "the credential body is built in one place"
    );
    // The search really ran, and the walker really reaches the deep files: a
    // reader that could not open the tree returns nothing, which would read as
    // a crate with no secrets in it at all.
    assert!(production_files_mentioning("Secret").contains(&"ui/views/settings.rs".to_string()));
}

#[test]
fn there_is_one_box_a_credential_is_typed_into_however_it_was_opened() {
    // The startup door's third step is a *call* to SETTINGS' own login form,
    // never a second form beside it. Pinned by module path rather than by
    // behaviour because that is the thing that can go wrong quietly: a door
    // that grew two masked fields of its own would draw identically, pass every
    // frame test, and give this crate a second answer to each of the rules the
    // one form carries — the masking, the `Drop` that wipes the buffers, the
    // owner's consent question about destroying a stored profile, and the
    // single file the plaintext is readable in above.
    for owned in ["Stage::Consent", "field_row", "secret::wipe"] {
        assert_eq!(
            production_files_mentioning(owned),
            vec!["ui/views/settings.rs".to_string()],
            "{owned} belongs to the one login form"
        );
    }
    // And the door reaches it by name, holding nothing itself: the handoff is
    // one call, so there is no state in which the door is carrying a pair.
    let door = source("ui/door.rs");
    assert!(
        door.contains("views.open_login()"),
        "the door opens no form"
    );
    assert!(
        !door.contains("Secret"),
        "the door holds something it should have handed over"
    );
}

#[test]
fn every_surface_that_offers_a_model_reads_the_one_producer() {
    // Three surfaces now put the same list in front of an operator — the
    // `/model` strip, the startup door's second question, and SETTINGS' model
    // switcher — and the honesty rules on that list are not obvious ones: a
    // backend the desk cannot reach stays *on* it with the owner's own
    // sentence and no choice behind it, and the workforce is offered `claude`
    // alone because the tier map owns its model.
    //
    // Pinned by module path rather than by behaviour, for the reason the login
    // form is: a surface that built its own list would render identically on
    // the day it was written and would drift on the first rule that changed —
    // and it would drift *quietly*, because each surface's own tests would keep
    // passing against its own copy.
    assert_eq!(
        production_files_mentioning("fn offers("),
        vec!["cmd.rs".to_string()],
        "the offers list is produced in one place"
    );
    assert_eq!(
        production_files_mentioning("cmd::offers("),
        vec!["ui/door.rs".to_string(), "ui/views/settings.rs".to_string()],
        "a surface is drawing a model list it built itself"
    );
    // And the mark that says which row a surface is already running, which is
    // the other half of the same list: two copies of "which held pair equals
    // this offer" is two chances for one of them to mark the wrong row.
    assert_eq!(
        production_files_mentioning(".running(store"),
        vec!["ui/door.rs".to_string(), "ui/views/settings.rs".to_string()],
    );
    // The searches really ran: a walker that could not read the tree returns
    // nothing, which would read as a crate with no model surfaces at all.
    assert!(production_files_mentioning("ModelChoice::Pair").contains(&"cmd.rs".to_string()));
}

// -- the glass build --------------------------------------------------------

#[cfg(not(feature = "operator"))]
mod glass {
    /// A compile-time assertion, not a runtime one: if this leg were ever built
    /// with the feature on, the crate would not compile rather than reporting a
    /// failure after the fact. Which is the right severity — a "glass" build
    /// that turned out to carry the writer is not a failing test, it is a
    /// binary that should not exist.
    const _THIS_LEG_IS_GLASS: () = assert!(!cfg!(feature = "operator"));

    #[test]
    fn the_default_build_has_no_write_module_to_reach() {
        // `atlas::net::write` cannot be named in this leg — the module is not in
        // the crate — so what is left to check is that the file the gate removes
        // is really the file that holds the writer, and that it says why.
        let src = super::source("net/write.rs");
        assert!(
            src.contains("pub struct WriteClient"),
            "the gated module is the one that holds the writer"
        );
        assert!(
            src.contains("holds there by absence"),
            "the write module states the invariant its gate preserves"
        );
    }

    #[test]
    fn neither_hand_off_to_the_claude_cli_exists_in_a_monitoring_build() {
        // A child process is not a `net::write`, so the censuses above would
        // never have noticed these two — and they are the only keys on this
        // workstation whose effect is a process rather than a request. Pinned
        // the way the write half is pinned: on the text of the gate, verbatim,
        // because a `cfg` naming the wrong feature compiles cleanly and nothing
        // else here would see it.
        let cmd = super::source("cmd.rs");
        for gate in [
            "#[cfg(feature = \"operator\")]\n    OpenCli,",
            "#[cfg(feature = \"operator\")]\n    OpenBuild(String),",
            "#[cfg(feature = \"operator\")]\n    Cli,",
            "#[cfg(feature = \"operator\")]\n    Build(String),",
        ] {
            assert!(
                cmd.contains(gate),
                "cmd.rs must gate the hand-offs on the operator feature, verbatim: {gate:?}"
            );
        }
        // And the thing that would act on one is gated too. The module itself
        // is in both builds — `Child` is a description, and the grammar is one
        // grammar — but `run`, which spawns, is not.
        assert!(
            super::source("handoff.rs").contains("#[cfg(feature = \"operator\")]\npub fn run("),
            "the hand-off that spawns is gated with the commands that reach it"
        );
        // The scopes are still *spelled* in this build, and must still be
        // unusable: a glass window is offered neither, and typing one in full
        // is refused rather than silently doing nothing.
        use atlas::cmd::{parse, resolve, suggestions, Resolved};
        use atlas::store::{Posture, Store};
        let store = Store::default();
        let offered = suggestions(&parse(""), &store, Posture::Glass);
        for word in ["/cli", "/build"] {
            assert!(
                !offered.iter().any(|s| s.value == word),
                "{word} was offered to a monitoring build"
            );
        }
        for line in ["/cli ", "/build add a visual"] {
            match resolve(&parse(line), &store, Posture::Glass) {
                Resolved::Refused(said) => assert!(said.contains("not armed"), "{line}: {said}"),
                other => panic!("{line}: {other:?}"),
            }
        }
    }

    #[test]
    fn the_pane_that_runs_a_child_is_not_in_a_monitoring_build() {
        // `atlas::pty` cannot be named in this leg — the module is not in the
        // crate — so what is left to check is that the gate is spelled the way
        // the compiler reads it, and that the file it removes is really the one
        // that opens a pseudoterminal and puts a process on it. A `cfg` naming
        // the wrong feature compiles cleanly in both legs, and nothing else
        // here would see it.
        assert!(
            super::source("lib.rs").contains("#[cfg(feature = \"operator\")]\npub mod pty;"),
            "lib.rs must gate `pty` on the operator feature, verbatim"
        );
        let pty = super::source("pty.rs");
        for held in [
            "openpty(",
            "spawn_command(",
            "pub fn write(",
            "pub fn kill(",
        ] {
            assert!(
                pty.contains(held),
                "the gated module is the one that owns the child: {held:?}"
            );
        }
        // And the seam that would give a child a column of the desk, on the
        // same terms: `atlas::pane` is not in this crate either, so what is
        // checked is the gate's own text and that the file it removes is the
        // one deciding when ATLAS stops being a chat.
        assert!(
            super::source("lib.rs").contains("#[cfg(feature = \"operator\")]\npub mod pane;"),
            "lib.rs must gate `pane` on the operator feature, verbatim"
        );
        let pane = super::source("pane.rs");
        for held in ["pub fn open(", "pub fn resized(", "store.open_pty("] {
            assert!(
                pane.contains(held),
                "the gated module is the one that opens the column: {held:?}"
            );
        }
    }

    #[test]
    fn a_monitoring_window_is_offered_neither_the_word_nor_the_pane() {
        // The word: `/cli` is a write scope, so the picker offers it to no
        // window this artifact can produce — there is no `Posture::Operator`
        // here to ask the other half of the question with, which is the whole
        // of what makes this an absence rather than a disabled key.
        use atlas::cmd::{parse, suggestions};
        use atlas::store::{Posture, Store};
        let offered = suggestions(&parse(""), &Store::default(), Posture::Glass);
        assert!(
            !offered.iter().any(|s| s.value == "/cli"),
            "a monitoring window was offered the word that opens a pane"
        );
        // And the strip really had something to offer, or the check above
        // passes on an empty list.
        assert!(offered.iter().any(|s| s.value == "/view"));
        // The pane: the branch that would draw one is gated, so the column is
        // not something this build can be argued into.
        assert!(
            super::source("ui/views/atlas.rs").contains(
                "#[cfg(feature = \"operator\")]\n        if let Some(screen) = store.pty_screen() {"
            ),
            "the column that draws a child must be gated, verbatim"
        );
    }

    #[test]
    fn a_monitoring_build_has_no_booking_key_no_word_and_no_box() {
        // Absence at every layer, checked where each one lives.
        //
        // The keymap first: `b` is a `writes` row, so the overlay a monitoring
        // window renders does not offer it — and this leg has no
        // `Posture::Operator` to ask the other question with.
        use atlas::input::{bindings, Binding};
        use atlas::store::{Posture, ViewId};
        let offered: Vec<&Binding> = bindings(Posture::Glass)
            .filter(|b| b.code == "Char('b')")
            .collect();
        assert!(
            offered.is_empty(),
            "a monitoring window was offered the booking key: {offered:?}"
        );
        // The table still *carries* the rows, in both builds, so this leg is
        // checking the same source text the armed one does rather than a
        // smaller table that would pass for the wrong reason.
        for view in [ViewId::Book, ViewId::Atlas] {
            assert!(
                atlas::input::KEYMAP
                    .iter()
                    .any(|b| b.code == "Char('b')" && b.source == atlas::input::Source::View(view)),
                "{view:?} lost its booking row from the table"
            );
        }
        // Then the module that would act on it. `atlas::net::write` cannot be
        // named in this leg at all, so what is left to check is that the file
        // the gate removes is really the one holding the booking call.
        assert!(
            super::source("net/write.rs").contains("pub async fn book(&self, token: BookToken)"),
            "the gated module is the one that books"
        );
    }

    #[test]
    fn a_monitoring_build_has_no_revoke_key_and_no_route_to_revoke_with() {
        // The AUTHORITY card is *read* in this build — what may book without a
        // human is exactly what a monitoring window is for — and none of it can
        // be touched. Absence at every layer, checked where each one lives.
        //
        // The keymap first: `R` on SETTINGS is a `writes` row, so the overlay a
        // monitoring window renders does not offer it, and this leg has no
        // `Posture::Operator` to ask the other question with.
        use atlas::input::{bindings, Binding, Source, KEYMAP};
        use atlas::store::{Posture, ViewId};
        let offered: Vec<&Binding> = bindings(Posture::Glass)
            .filter(|b| b.code == "Char('R')" && b.source == Source::View(ViewId::Settings))
            .collect();
        assert!(
            offered.is_empty(),
            "a monitoring window was offered the revoke key: {offered:?}"
        );
        // The table still *carries* the row, in both builds, so this leg checks
        // the same source text the armed one does rather than a smaller table
        // that would pass for the wrong reason.
        assert!(
            KEYMAP.iter().any(|b| b.code == "Char('R')"
                && b.source == Source::View(ViewId::Settings)
                && b.action.contains("revoke")),
            "SETTINGS lost its revoke row from the table"
        );
        // Then the command, which is gated verbatim — a `cfg` naming the wrong
        // feature compiles cleanly in both legs and nothing else here would see
        // it.
        assert!(
            super::source("cmd.rs")
                .contains("#[cfg(feature = \"operator\")]\n    RevokeAuthority {"),
            "cmd.rs must gate the revocation on the operator feature, verbatim"
        );
        // And the module that would act on it. `atlas::net::write` cannot be
        // named in this leg at all, so what is left to check is that the file
        // the gate removes is really the one holding the revoke call — and that
        // there is no grant call beside it, in either build.
        let writer = super::source("net/write.rs");
        assert!(
            writer.contains("pub async fn revoke_authority(&self, reason: &str)"),
            "the gated module is the one that revokes"
        );
        assert!(
            !writer.contains("fn grant_authority"),
            "this client composes no grant: every ceiling is a number it may not default"
        );
    }

    #[test]
    fn the_posture_a_glass_build_can_hold_is_glass_and_only_glass() {
        // One inhabitant, not one branch. `Posture::Operator` does not exist in
        // this build, so there is no value a bug could assign that would put the
        // amber word on the status line of a monitoring box.
        use atlas::store::Posture;
        assert_eq!(Posture::default(), Posture::Glass);
        assert_eq!(Posture::default().label(), "GLASS");
    }
}

// -- the operator build -----------------------------------------------------

#[cfg(feature = "operator")]
mod operator {
    use atlas::model::Snapshot;
    use atlas::net::write::{Authority, Board, Booked, Choice, Execution, Rights, WriteClient};
    use atlas::store::Posture;
    use atlas::ui::widgets::confirm::Modal;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    /// One request the client made, as the owner saw it.
    #[derive(Debug, Clone, PartialEq, Eq)]
    struct Seen {
        method: String,
        path: String,
        body: String,
    }

    struct Owner {
        base: String,
        seen: Arc<Mutex<Vec<Seen>>>,
    }

    impl Owner {
        fn only(&self) -> Seen {
            let seen = self.seen.lock().unwrap();
            assert_eq!(seen.len(), 1, "expected exactly one request: {seen:?}");
            seen[0].clone()
        }
    }

    /// A canned owner that answers every request with `status` and `body`, and
    /// records the method, path, and body it was sent.
    ///
    /// A real socket rather than a mocked transport, for the reason
    /// `http_poll.rs` gives: the thing worth pinning is that the bytes an owner
    /// would actually dispatch on are the bytes this client writes. A faked
    /// `reqwest` layer would pin the fake.
    /// `body` is taken by value rather than as `&'static str`: the desk-mode
    /// answers below vary one field at a time, and a canned owner that could
    /// only serve a literal would have each of those spelled out in full.
    fn spawn_owner(status: u16, body: impl Into<String>) -> Owner {
        let body = body.into();
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
        let base = format!("http://{}", listener.local_addr().unwrap());
        let seen = Arc::new(Mutex::new(Vec::new()));
        let recorded = Arc::clone(&seen);

        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { return };
                let Ok(peek) = stream.try_clone() else {
                    continue;
                };
                let mut reader = BufReader::new(peek);

                let mut request_line = String::new();
                if reader.read_line(&mut request_line).is_err() || request_line.is_empty() {
                    continue;
                }
                let mut parts = request_line.split_whitespace();
                let method = parts.next().unwrap_or("").to_string();
                let path = parts.next().unwrap_or("/").to_string();

                // Headers, then exactly as many body bytes as were announced. A
                // read to EOF would block: the client keeps the socket open for
                // the response it is waiting for.
                let mut length = 0usize;
                loop {
                    let mut header = String::new();
                    match reader.read_line(&mut header) {
                        Ok(0) | Err(_) => break,
                        Ok(_) if header == "\r\n" => break,
                        Ok(_) => {
                            let lower = header.to_ascii_lowercase();
                            if let Some(value) = lower.strip_prefix("content-length:") {
                                length = value.trim().parse().unwrap_or(0);
                            }
                        }
                    }
                }
                let mut buf = vec![0u8; length];
                if length > 0 && reader.read_exact(&mut buf).is_err() {
                    continue;
                }
                recorded.lock().unwrap().push(Seen {
                    method,
                    path,
                    body: String::from_utf8_lossy(&buf).to_string(),
                });

                let response = format!(
                    "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\n\
                     content-length: {}\r\nconnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes());
                let _ = stream.flush();
            }
        });

        Owner { base, seen }
    }

    fn snapshot() -> Snapshot {
        serde_json::from_str(include_str!("fixtures/tui_snapshot.json")).unwrap()
    }

    /// The fixture's checked plan and the approval that covers it.
    fn checked_plan() -> (atlas::model::Plan, atlas::model::Approval) {
        let snap = snapshot();
        (snap.plans[0].clone(), snap.approvals[0].clone())
    }

    // -- the posture ------------------------------------------------------

    #[test]
    fn an_operator_build_can_say_either_word() {
        assert_eq!(
            Posture::default(),
            Posture::Glass,
            "the flag arms it, not the feature"
        );
        assert_eq!(Posture::Operator.label(), "OPERATOR");
        assert_eq!(Posture::Glass.label(), "GLASS");
    }

    #[test]
    fn the_status_line_says_operator_only_once_the_flag_armed_it() {
        // The chip is the operator's one continuous answer to "can this window
        // place an order". A featured build that was not armed must still read
        // GLASS, or the word means "which binary" instead of "what can happen
        // next".
        use atlas::store::Store;
        let mut store = Store::default();
        assert!(frame(&store).contains("GLASS"));
        store.posture = Posture::Operator;
        let armed = frame(&store);
        assert!(armed.contains("OPERATOR"), "{armed}");
        assert!(!armed.contains("GLASS"), "{armed}");
    }

    /// A store that has heard one snapshot, carrying the posture the owner
    /// persisted — the only way this client learns what it may do.
    fn store_with_posture(armed: Option<bool>) -> atlas::store::Store {
        let mut store = atlas::store::Store::default();
        let posture = match armed {
            Some(armed) => serde_json::json!({"armed": armed, "chosen": true}),
            None => serde_json::Value::Null,
        };
        store.apply(
            atlas::bus::AppEvent::Snapshot(Box::new(
                serde_json::from_value(serde_json::json!({"posture": posture})).unwrap(),
            )),
            std::time::Instant::now(),
        );
        // The desk-mode door would otherwise be up — this payload names no
        // desk — and a door swallows the keystroke that opens the palette.
        store.settle_door();
        store
    }

    /// The palette's scope strip, which is where a write scope is advertised.
    fn scopes_of(mut store: atlas::store::Store) -> String {
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
        let mut views = atlas::ui::views::Views::new();
        atlas::ui::shell::on_key(
            KeyEvent::new(KeyCode::Char('/'), KeyModifiers::NONE),
            &mut store,
            &mut views,
        );
        frame_with(&store, &views)
    }

    #[test]
    fn an_unarmed_desk_offers_nothing_even_in_a_capable_binary() {
        // The consequence, not the enum. This binary has every write path
        // compiled in; a desk the operator answered "read-only" for must still
        // read GLASS and must not advertise a scope it will refuse.
        let frame = frame(&store_with_posture(Some(false)));
        assert!(frame.contains("GLASS"), "{frame}");
        let offered = scopes_of(store_with_posture(Some(false)));
        assert!(
            !offered.contains("/mode"),
            "an unarmed desk must not advertise a write scope:\n{offered}"
        );
        // A desk nobody has been asked about is the same refusal for a
        // different reason — absence is not consent.
        let unasked = scopes_of(store_with_posture(None));
        assert!(!unasked.contains("/mode"), "{unasked}");
    }

    #[test]
    fn an_armed_desk_is_what_puts_the_write_scope_on_the_strip() {
        // The other side of the pin above: without this the negative assertion
        // would pass on a frame that never offers `/mode` to anyone.
        let frame = frame(&store_with_posture(Some(true)));
        assert!(frame.contains("OPERATOR"), "{frame}");
        let offered = scopes_of(store_with_posture(Some(true)));
        assert!(offered.contains("/mode"), "{offered}");
    }

    fn frame(store: &atlas::store::Store) -> String {
        frame_with(store, &atlas::ui::views::Views::new())
    }

    /// One frame drawn from views a test has already pressed keys into — the
    /// only way to read what a *surface* did with an outcome, rather than what
    /// the outcome said.
    fn frame_with(store: &atlas::store::Store, views: &atlas::ui::views::Views) -> String {
        use atlas::fx::Fx;
        let mut term = ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 36)).unwrap();
        let fx = Fx::default();
        let now = std::time::Instant::now();
        term.draw(|f| atlas::ui::shell::draw(f, store, views, &fx, now))
            .unwrap();
        term.backend()
            .buffer()
            .content()
            .chunks(120)
            .map(|row| row.iter().map(|c| c.symbol()).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n")
    }

    // -- the modal contract -----------------------------------------------

    #[test]
    fn a_plan_modal_challenges_with_the_last_six_of_the_owners_targets_hash() {
        // The binding that makes this more than a keystroke. The referee's PASS
        // is bound to the exact `targets_hash`; so is the human's confirmation,
        // and the six characters are only ever on screen inside the modal — a
        // scripted replay of "y" cannot produce them, and a replay captured
        // against yesterday's plan produces the wrong six.
        let (plan, approval) = checked_plan();
        let modal = Modal::for_plan(&plan, &approval).expect("the approval covers the plan");
        assert_eq!(approval.targets_hash.as_deref(), Some("c4d5e6f708192a3b"));
        assert_eq!(modal.challenge(), "192a3b");
    }

    #[test]
    fn nothing_short_of_the_exact_challenge_mints_a_token() {
        let (plan, approval) = checked_plan();
        let mut modal = Modal::for_plan(&plan, &approval).unwrap();
        assert!(modal.token().is_none(), "an untouched modal is not armed");

        for c in "192a3".chars() {
            modal.push(c);
        }
        assert!(modal.token().is_none(), "a prefix is not the challenge");

        modal.push('X');
        assert!(
            modal.token().is_none(),
            "a wrong character is not the challenge"
        );
        modal.backspace();
        modal.push('b');

        let token = modal.token().expect("the exact challenge arms the modal");
        assert_eq!(token.plan_id(), "9661b0e88b4a669e");
        assert_eq!(token.approval_id(), "1a2b3c4d5e6f7081");
        assert_eq!(token.targets_hash(), "c4d5e6f708192a3b");
    }

    #[test]
    fn one_accepted_modal_yields_exactly_one_confirmation() {
        // Single use has to sit on the *consent*, not on the token. `token()`
        // minting on `&self` meant `loop { c.execute_plan(m.token().unwrap()) }`
        // compiled: the human confirmed once and the client could book any
        // number of times. Minting now spends the modal, so a second attempt
        // gets nothing to send.
        let (plan, approval) = checked_plan();
        let mut modal = Modal::for_plan(&plan, &approval).unwrap();
        for c in "192a3b".chars() {
            modal.push(c);
        }
        assert!(
            modal.token().is_some(),
            "the first mint is the confirmation"
        );
        assert!(
            modal.token().is_none(),
            "a spent modal must not mint a second confirmation"
        );
        // And it stays spent: retyping the challenge into a modal whose consent
        // was already used must not re-arm it.
        modal.backspace();
        modal.push('b');
        assert!(modal.token().is_none(), "a spent modal cannot be re-armed");
    }

    #[test]
    fn the_modal_shows_the_leg_count_the_owners_gate_will_check() {
        // The owner's `execute_plan_with_approval` takes `expected_legs` from
        // `stored["pre_trade"]["n_legs"]` (`server.py:1895`) and refuses the
        // plan if the persisted legs disagree. The approval's `summary` is a
        // different number written at a different time — in this fixture it says
        // 7 while the plan really has 2 — so showing the summary asked a human
        // to vouch for a seven-leg trade that the gate would evaluate as two.
        // The box must state what the gate will check.
        let (plan, approval) = checked_plan();
        assert_eq!(
            approval.summary.as_ref().unwrap()["n_legs"],
            serde_json::json!(7),
            "fixture guard: the approval summary must disagree, or this proves nothing"
        );
        assert_eq!(
            plan.pre_trade.as_ref().unwrap()["n_legs"],
            serde_json::json!(2)
        );

        let modal = Modal::for_plan(&plan, &approval).unwrap();
        let shown = modal.facts();
        let legs = shown
            .iter()
            .find(|(label, _)| label == "legs")
            .map(|(_, value)| value.clone());
        assert_eq!(legs, Some("2".to_string()), "shown facts: {shown:?}");

        // The hash still comes from the approval — that is the fact the approval
        // genuinely owns, and the one the referee's PASS is bound to.
        let hash = shown.iter().find(|(label, _)| label == "targets hash");
        assert_eq!(
            hash.map(|(_, v)| v.as_str()),
            Some("c4d5e6f708192a3b"),
            "shown facts: {shown:?}"
        );
    }

    #[test]
    fn an_approval_for_another_plan_cannot_be_used_to_confirm_this_one() {
        // The join is the governance-critical half. Binding the modal to an
        // approval that covers a *different* plan would show six characters that
        // arm an execution the human never reviewed — the exact substitution
        // `targets_hash` exists to prevent, reintroduced at the client.
        let (plan, mut approval) = checked_plan();
        approval.plan_id = Some("0000000000000000".into());
        assert!(Modal::for_plan(&plan, &approval).is_none());
    }

    #[test]
    fn a_plan_with_no_owner_computed_hash_cannot_be_confirmed_at_all() {
        // Refuse, never substitute. Falling back to the plan id would put a
        // six-character challenge on screen that binds to nothing the referee
        // ever checked — a confirmation ritual with no content, which is worse
        // than no ritual.
        let (plan, mut approval) = checked_plan();
        approval.targets_hash = None;
        assert!(Modal::for_plan(&plan, &approval).is_none());
        approval.targets_hash = Some("short".into());
        assert!(Modal::for_plan(&plan, &approval).is_none());
    }

    #[test]
    fn an_action_modal_challenges_with_the_static_word() {
        // Atlas's mode has no plan to bind to, so there is no hash to echo. It
        // still takes a typed word: the point is that the keystroke that changes
        // what Atlas may do is never one key away.
        let mut modal = Modal::action("SET ATLAS MODE", vec![("mode".into(), "propose".into())]);
        assert_eq!(modal.challenge(), "CONFIRM");
        for c in "CONFIRM".chars() {
            modal.push(c);
        }
        assert!(modal.armed());
        // An action modal binds no plan, so it can never mint the capability
        // that reaches `execute_plan`.
        assert!(
            modal.token().is_none(),
            "an action modal is not a plan token"
        );
    }

    #[test]
    fn the_modal_is_fifty_by_twelve_and_shows_the_facts_it_binds() {
        let (plan, approval) = checked_plan();
        let modal = Modal::for_plan(&plan, &approval).unwrap();
        let mut term = ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 36)).unwrap();
        term.draw(|f| modal.draw(f, f.area())).unwrap();
        let text: Vec<String> = term
            .backend()
            .buffer()
            .content()
            .chunks(120)
            .map(|row| row.iter().map(|c| c.symbol()).collect::<String>())
            .collect();
        let painted: Vec<usize> = text
            .iter()
            .enumerate()
            .filter(|(_, row)| row.trim().len() > 1)
            .map(|(i, _)| i)
            .collect();
        assert_eq!(painted.len(), 12, "the modal is twelve rows: {painted:?}");
        let body = text.join("\n");
        assert!(
            body.contains("9661b0e88b4a669e"),
            "the plan id is a fact: {body}"
        );
        assert!(
            body.contains("c4d5e6f708192a3b"),
            "the hash is a fact: {body}"
        );
        // The challenge is shown only here — that is what makes typing it proof
        // the human looked at this plan.
        assert!(
            body.contains("192a3b"),
            "the challenge is in the modal: {body}"
        );
    }

    // -- the writer -------------------------------------------------------

    #[tokio::test]
    async fn approve_and_reject_hit_the_owners_two_verbs() {
        let owner = spawn_owner(200, r#"{"status": "approved"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        client.approve("1a2b3c4d5e6f7081").await.unwrap();
        assert_eq!(
            owner.only(),
            Seen {
                method: "POST".into(),
                path: "/api/approvals/1a2b3c4d5e6f7081/approve".into(),
                body: "{}".into(),
            }
        );

        let owner = spawn_owner(200, r#"{"status": "rejected"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        client.reject("1a2b3c4d5e6f7081").await.unwrap();
        assert_eq!(owner.only().path, "/api/approvals/1a2b3c4d5e6f7081/reject");
    }

    #[tokio::test]
    async fn execution_carries_the_confirmation_the_owner_refuses_to_book_without() {
        // The owner returns 400 unless the body carries `human_confirmed: true`
        // *and* an `approval_id`: "a bare human_confirmed flag cannot book a
        // trade". Both come from the token, so neither can be supplied by a
        // caller that never opened the modal.
        let (plan, approval) = checked_plan();
        let mut modal = Modal::for_plan(&plan, &approval).unwrap();
        for c in "192a3b".chars() {
            modal.push(c);
        }
        let token = modal.token().unwrap();

        let owner = spawn_owner(200, r#"{"executed": true, "filled": 2}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        let outcome = client.execute_plan(token).await.unwrap();
        assert!(matches!(outcome, Execution::Executed(_)), "{outcome:?}");

        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/plans/execute");
        let body: serde_json::Value = serde_json::from_str(&seen.body).unwrap();
        assert_eq!(body["human_confirmed"], serde_json::json!(true));
        assert_eq!(body["plan_id"], serde_json::json!("9661b0e88b4a669e"));
        assert_eq!(body["approval_id"], serde_json::json!("1a2b3c4d5e6f7081"));
    }

    /// A booking capability, minted the way the desk mints one: a box that
    /// displays the hash, and Enter.
    fn book_token() -> atlas::ui::widgets::confirm::BookToken {
        let mut modal = Modal::book("b92a58fa5c1d4e7f", "0f1e2d3c4b5a6978", vec![]).unwrap();
        modal.book_token().unwrap()
    }

    #[tokio::test]
    async fn a_booking_carries_the_hash_the_box_displayed_and_no_approval_id() {
        // The owner's route resolves the current proposal itself and refuses a
        // plan that is not it, so naming an approval here would be this client
        // choosing which question it is answering. What it does send is the
        // human's confirmation and the hash the box put on screen — neither of
        // which a caller can vary, because both come out of the token.
        let owner = spawn_owner(
            200,
            r#"{"booked": true, "approval_id": "5e92e0d9",
                "execution": {"executed": true, "orders": [1, 2, 3]}}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let outcome = client.book(book_token()).await.unwrap();
        assert!(matches!(outcome, Booked::Filled(_)), "{outcome:?}");

        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/desk/proposal/book");
        let body: serde_json::Value = serde_json::from_str(&seen.body).unwrap();
        assert_eq!(body["human_confirmed"], serde_json::json!(true));
        assert_eq!(body["plan_id"], serde_json::json!("b92a58fa5c1d4e7f"));
        assert_eq!(body["targets_hash"], serde_json::json!("0f1e2d3c4b5a6978"));
        assert!(body.get("approval_id").is_none(), "{body}");
    }

    #[tokio::test]
    async fn the_three_shapes_of_a_refused_booking_are_three_different_answers() {
        // F2's corrected contract, and the reason this is a four-variant enum:
        // a `blocked_by == "approval"` withdrew the approval, while a data
        // revalidation and a mandate violation left it standing. A client that
        // read every `booked: false` as "re-propose" throws away a live
        // approval in two cases out of three.
        let cases: [(&str, &str); 3] = [
            (
                r#"{"booked": false, "execution": {"executed": false, "blocked_by": "approval",
                    "reasons": ["book moved since approval (revision mismatch)"]}}"#,
                "invalidated",
            ),
            (
                r#"{"booked": false, "execution": {"executed": false,
                    "blocked_by": "data_revalidation", "data_health": {"stale": 3}}}"#,
                "standing",
            ),
            (
                r#"{"booked": false, "execution": {"executed": false,
                    "mandate_violation": "single-name cap exceeded on XLK"}}"#,
                "standing",
            ),
        ];
        for (body, expected) in cases {
            let owner = spawn_owner(200, body);
            let client = WriteClient::new(&owner.base).unwrap();
            let outcome = client.book(book_token()).await.unwrap();
            let (got, reasons) = match &outcome {
                Booked::Invalidated { reasons, .. } => ("invalidated", reasons),
                Booked::Standing { reasons, .. } => ("standing", reasons),
                Booked::Unstated { reasons, .. } => ("unstated", reasons),
                Booked::Filled(_) => panic!("a refusal was read as a fill: {body}"),
            };
            assert_eq!(got, expected, "{body}");
            // Never empty: a refusal an operator cannot read is not actionable,
            // and the `data_revalidation` shape carries no `reasons` list at
            // all — its own health object is the reason of last resort.
            assert!(!reasons.is_empty(), "{body}");
        }
    }

    #[tokio::test]
    async fn a_blocker_the_owner_did_not_name_is_neither_guess() {
        // Both defaults cost something: "re-propose" throws away an approval
        // that may still be live, "retry" sends a human back at a question that
        // no longer exists. Invariant 4 — say the answer could not be read.
        let owner = spawn_owner(
            200,
            r#"{"booked": false, "execution": {"executed": false, "blocked_by": "something_new"}}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert!(matches!(
            client.book(book_token()).await.unwrap(),
            Booked::Unstated { .. }
        ));
    }

    #[tokio::test]
    async fn a_body_that_does_not_say_whether_it_booked_is_refused_not_assumed() {
        // The same floor `executed` has on the other route: both guesses are
        // indefensible — one invents a fill, the other hides one.
        let owner = spawn_owner(200, r#"{"approval_id": "5e92e0d9"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        let err = client.book(book_token()).await.unwrap_err();
        assert!(
            err.to_string().contains("without saying whether it booked"),
            "{err}"
        );
    }

    #[tokio::test]
    async fn the_owners_own_refusal_sentence_is_what_a_four_hundred_carries() {
        // The route's own refusals are 400s — "not the current proposal", a
        // hash mismatch, no covering PASS — and the sentence is what tells the
        // operator what to do next.
        let owner = spawn_owner(400, r#"{"error": "not the current proposal"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        let err = client.book(book_token()).await.unwrap_err();
        assert!(
            err.to_string().contains("not the current proposal"),
            "{err}"
        );
    }

    /// An armed token, for the outcome tests below.
    /// `AppEvent` has no `Debug` — the bus carries a typed credential — so a
    /// failing assertion names the variant rather than dumping the value.
    fn debug(ev: &AppEvent) -> &'static str {
        match ev {
            AppEvent::Wrote(_) => "a write outcome that is not a refusal",
            _ => "some other bus event",
        }
    }

    fn armed_token() -> atlas::ui::widgets::confirm::ConfirmToken {
        let (plan, approval) = checked_plan();
        let mut modal = Modal::for_plan(&plan, &approval).unwrap();
        for c in "192a3b".chars() {
            modal.push(c);
        }
        modal.token().unwrap()
    }

    #[tokio::test]
    async fn a_refused_fill_is_not_reported_as_a_booked_one() {
        // The bug this pins: the execution gate declines with **HTTP 200** and
        // `executed: false` — `server.py:2629` returns `200, result` whatever
        // the result, and the handler comment at :2613 says so. A client that
        // only errored on non-2xx therefore reported every governance refusal
        // as a successful fill, which is the single worst thing this surface
        // could tell an operator.
        //
        // Refusal is a third outcome: not an `Err` (the desk answered, and the
        // answer is legitimate) and emphatically not an `Ok(Executed)`.
        let owner = spawn_owner(
            200,
            r#"{"executed": false, "blocked_by": "approval",
                "reasons": ["approval has expired", "book moved since approval (revision mismatch)"]}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match client.execute_plan(armed_token()).await.unwrap() {
            Execution::Refused {
                blocked_by,
                reasons,
            } => {
                assert_eq!(blocked_by, "approval");
                assert_eq!(reasons.len(), 2);
                assert!(reasons[0].contains("expired"), "{reasons:?}");
            }
            other => panic!("a refused fill must not read as booked: {other:?}"),
        }
    }

    #[tokio::test]
    async fn every_shape_the_gate_declines_with_is_a_refusal_and_none_is_a_fill() {
        // Four `executed: false` shapes exist in `server.py`, and one of them
        // carries **no** `blocked_by` at all (`:1909`, the mandate violation).
        // A client that keyed on that field alone would fall through to
        // "success" on exactly the refusal that means the plan broke the
        // mandate.
        let cases: Vec<(&'static str, &str)> = vec![
            (
                r#"{"executed": false, "blocked_by": "approval", "reasons": ["no approval record"]}"#,
                "approval",
            ),
            (
                r#"{"executed": false, "blocked_by": "data_revalidation", "data_health": {"blocked": true}}"#,
                "data_revalidation",
            ),
            (
                r#"{"executed": false, "mandate_violation": "position cap breached"}"#,
                "mandate_violation",
            ),
        ];
        for (body, want) in cases {
            let owner = spawn_owner(200, body);
            let client = WriteClient::new(&owner.base).unwrap();
            match client.execute_plan(armed_token()).await.unwrap() {
                Execution::Refused {
                    blocked_by,
                    reasons,
                } => {
                    assert_eq!(blocked_by, want, "{body}");
                    // Never silently empty: an operator told "refused" with no
                    // reason cannot act, and the mandate shape's reason is in a
                    // different key than the approval shape's.
                    assert!(!reasons.is_empty(), "a refusal must say why: {body}");
                }
                other => panic!("{body} must be a refusal, got {other:?}"),
            }
        }
    }

    #[tokio::test]
    async fn a_body_that_does_not_say_whether_it_executed_is_refused_not_assumed() {
        // The owner always sets `executed` on this route. A 200 without it is a
        // broken contract, and guessing either way is indefensible — one guess
        // invents a fill, the other hides one.
        let owner = spawn_owner(200, r#"{"approval_id": "1a2b3c4d5e6f7081"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        assert!(client.execute_plan(armed_token()).await.is_err());
    }

    #[tokio::test]
    async fn every_atlas_and_desk_verb_lands_on_the_route_the_owner_dispatches_on() {
        // One table rather than nine tests: the value is that each pair matches
        // `qlab/ui/server.py`'s dispatch, and a table makes a missing route
        // obvious rather than a missing test.
        let cases: Vec<(&str, &str, serde_json::Value)> = vec![
            (
                "/api/atlas/mode",
                "mode",
                serde_json::json!({"mode": "propose"}),
            ),
            ("/api/atlas/pause", "pause", serde_json::json!({})),
            (
                "/api/atlas/resume",
                "resume",
                serde_json::json!({"mode": "observe"}),
            ),
            (
                "/api/atlas/autonomy",
                "autonomy",
                serde_json::json!({"enabled": true}),
            ),
            (
                "/api/atlas/message",
                "message",
                serde_json::json!({"text": "why flat?"}),
            ),
            (
                "/api/workforce/fast",
                "fast",
                serde_json::json!({"enabled": false}),
            ),
            (
                "/api/desk_mode",
                "desk",
                serde_json::json!({"data": "synthetic", "book": "simulated_paper"}),
            ),
            (
                "/api/workflows/start",
                "workflow",
                serde_json::json!({"kind": "portfolio_review", "goal": "review the book"}),
            ),
            (
                "/api/desk/posture",
                "posture",
                serde_json::json!({"armed": true}),
            ),
        ];

        for (path, which, want) in cases {
            let owner = spawn_owner(200, r#"{"ok": true}"#);
            let client = WriteClient::new(&owner.base).unwrap();
            match which {
                "mode" => client.atlas_mode("propose").await.unwrap(),
                "pause" => client.atlas_pause().await.unwrap(),
                "resume" => client.atlas_resume("observe").await.unwrap(),
                "autonomy" => client.atlas_autonomy(true).await.unwrap(),
                "message" => client.atlas_message("why flat?").await.unwrap(),
                "fast" => client.workforce_fast(false).await.unwrap(),
                "desk" => client
                    .desk_mode("synthetic", "simulated_paper")
                    .await
                    .unwrap(),
                "workflow" => client
                    .start_workflow("portfolio_review", "review the book")
                    .await
                    .unwrap(),
                "posture" => client.set_posture(true).await.unwrap(),
                other => panic!("untested verb {other}"),
            };
            let seen = owner.only();
            assert_eq!(seen.method, "POST", "{path}");
            assert_eq!(seen.path, path);
            assert_eq!(
                serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
                want,
                "{path}"
            );
        }
    }

    #[tokio::test]
    async fn a_non_2xx_is_reported_with_the_owners_own_words() {
        // A real 400 from a route that really returns one: `decide_approval`
        // raises `PermissionError("approval is 'rejected', not pending")`, which
        // the dispatcher turns into a 400. The previous version of this test
        // fired execute-gate text at `approve()` — a refusal that route cannot
        // produce — which made the suite look like it covered the execution gate
        // while the gate's actual 200-shaped refusals went unchecked.
        let owner = spawn_owner(400, r#"{"error": "approval is 'rejected', not pending"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        let err = client.approve("1a2b3c4d5e6f7081").await.unwrap_err();
        let said = err.to_string();
        assert!(said.contains("400"), "{said}");
        assert!(said.contains("not pending"), "{said}");
    }

    #[tokio::test]
    async fn an_owner_that_is_not_there_is_an_error_and_not_a_silent_success() {
        // Port 1 on loopback: nothing listens, and the connect fails fast.
        let client = WriteClient::new("http://127.0.0.1:1").unwrap();
        assert!(client.atlas_pause().await.is_err());
    }

    // -- the dispatch seam -------------------------------------------------
    //
    // What turns a confirmed `Command` into a request, and what a write outcome
    // owes the poller. This lived in `main.rs`, where nothing could reach it:
    // the routing that decides which owner verb a keystroke lands on, and the
    // predicate that decides whether a *failed* write refreshes the desk, both
    // shipped with no test at all. Invariant 10, one layer above the seams it
    // usually catches.

    use atlas::bus::{AppEvent, Wrote};
    use atlas::cmd::Command;
    use atlas::dispatch::{perform, refetches, Writes};

    /// An armed token for the fixture plan, minted the only way one can be.
    fn token() -> atlas::ui::widgets::confirm::ConfirmToken {
        armed_token()
    }

    #[tokio::test]
    async fn each_write_command_lands_on_the_owner_verb_it_names() {
        // The routing itself. A `Reject` that reached `/approve` would be an
        // operator's refusal recorded as consent, and nothing downstream could
        // tell — both answer 200 with a status the client does not re-read.
        let owner = spawn_owner(200, r#"{"status": "approved"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, Command::Approve("1a2b3c4d5e6f7081".into())).await,
            Some(Wrote::Decided {
                approval_id: "1a2b3c4d5e6f7081".into(),
                decision: "approved",
            })
        );
        assert_eq!(owner.only().path, "/api/approvals/1a2b3c4d5e6f7081/approve");

        let owner = spawn_owner(200, r#"{"status": "rejected"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, Command::Reject("1a2b3c4d5e6f7081".into())).await,
            Some(Wrote::Decided {
                approval_id: "1a2b3c4d5e6f7081".into(),
                decision: "rejected",
            })
        );
        assert_eq!(owner.only().path, "/api/approvals/1a2b3c4d5e6f7081/reject");

        let owner = spawn_owner(200, r#"{"executed": true, "n_fills": 2}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, Command::Execute(token())).await,
            Some(Wrote::Executed {
                plan_id: "9661b0e88b4a669e".into(),
            })
        );
        let seen = owner.only();
        assert_eq!(seen.path, "/api/plans/execute");
        let body: serde_json::Value = serde_json::from_str(&seen.body).unwrap();
        assert_eq!(body["human_confirmed"], serde_json::json!(true));
        assert_eq!(body["approval_id"], serde_json::json!("1a2b3c4d5e6f7081"));
    }

    #[tokio::test]
    async fn a_booking_routes_to_the_desks_own_verb_and_its_outcome_survives_the_seam() {
        // Which method a `Command` reaches and which `Wrote` each answer
        // becomes is the part that decides what happens to money, and inside a
        // `tokio::spawn` it could only be observed through the bus. Both halves
        // here: the route, and the three-way split a card renders two different
        // sentences from.
        let owner = spawn_owner(
            200,
            r#"{"booked": true, "approval_id": "5e92e0d9",
                "execution": {"executed": true, "orders": [1, 2]}}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let outcome = perform(&client, Command::Book(book_token())).await;
        match outcome {
            Some(Wrote::Booked { plan_id, summary }) => {
                assert_eq!(plan_id, "b92a58fa5c1d4e7f");
                // The owner's own orders, never a receipt composed from what
                // was sent.
                assert!(summary.contains("2 orders"), "{summary}");
            }
            other => panic!("a fill did not survive the seam: {other:?}"),
        }
        assert_eq!(owner.only().path, "/api/desk/proposal/book");

        // And the refusal that leaves the approval standing keeps that fact:
        // `survives` is what the card turns into "retry" rather than
        // "re-propose", and losing it here would flatten the two.
        let owner = spawn_owner(
            200,
            r#"{"booked": false, "execution": {"executed": false,
                "mandate_violation": "single-name cap exceeded"}}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::Book(book_token())).await {
            Some(Wrote::BookRefused {
                blocked_by,
                survives,
                reasons,
                ..
            }) => {
                assert_eq!(blocked_by, "mandate_violation");
                assert_eq!(survives, Some(true));
                assert_eq!(reasons, vec!["single-name cap exceeded".to_string()]);
            }
            other => panic!("a refusal was not reported as one: {other:?}"),
        }
    }

    #[tokio::test]
    async fn a_gate_refusal_survives_the_seam_as_a_refusal() {
        // The 200-shaped decline, carried through the dispatch layer with the
        // owner's own words intact. Folded into `Executed` here it would reach
        // the toast and the card as a booked fill.
        let owner = spawn_owner(
            200,
            r#"{"executed": false, "blocked_by": "approval",
                "reasons": ["approval has expired"]}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, Command::Execute(token())).await,
            Some(Wrote::Refused {
                plan_id: "9661b0e88b4a669e".into(),
                blocked_by: "approval".into(),
                reasons: vec!["approval has expired".into()],
            })
        );
    }

    #[tokio::test]
    async fn a_request_that_never_landed_names_what_it_was_and_what_was_said() {
        // Port 1: nothing listens. The outcome has to name the plan it was
        // about — an audit trail ending at "a write failed" cannot be matched
        // to the key that was pressed.
        let client = WriteClient::new("http://127.0.0.1:1").unwrap();
        match perform(&client, Command::Execute(token())).await {
            Some(Wrote::Failed { what, said }) => {
                assert!(what.contains("9661b0e88b4a669e"), "{what}");
                assert!(!said.is_empty(), "a failure must carry the reason");
            }
            other => panic!("an unreachable owner is a failure, got {other:?}"),
        }
        let client = WriteClient::new("http://127.0.0.1:1").unwrap();
        match perform(&client, Command::Approve("a1".into())).await {
            Some(Wrote::Failed { what, .. }) => assert!(what.contains("a1"), "{what}"),
            other => panic!("{other:?}"),
        }
    }

    // -- the predictor run -------------------------------------------------

    #[tokio::test]
    async fn a_predictor_run_lands_on_the_route_the_owner_dispatches_on() {
        // The route, the body keys, and the answer, through the dispatch seam
        // rather than the client alone: which method a `Command` reaches and
        // which `Wrote` each answer becomes is the half a view cannot pin.
        let owner = spawn_owner(
            200,
            r#"{"run_id": "9f3c1d77aa20", "models": ["kernel:zz", "ridge:none"],
                "champion": "kernel:zz", "ranking": ["kernel:zz", "ridge:none"]}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(
                &client,
                Command::RunPredictor {
                    model: "kernel:zz".into(),
                    offline: true,
                },
            )
            .await,
            Some(Wrote::PredictorRan {
                run_id: "9f3c1d77aa20".into(),
                // The owner's own list, which carries the baseline it appended
                // rather than the one lane that was asked for.
                models: vec!["kernel:zz".into(), "ridge:none".into()],
                champion: Some("kernel:zz".into()),
            })
        );
        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/research/predictors/run");
        // The two keys H1's contract requires, and nothing else: `universe`
        // and `lookback_days` are left to the route's own defaults, because
        // this client has no surface that chooses them.
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
            serde_json::json!({"model": "kernel:zz", "offline": true})
        );
    }

    #[tokio::test]
    async fn a_lane_the_owner_does_not_serve_is_a_refusal_and_not_a_failure() {
        // The 400 an operator will actually hit, and it names every lane the
        // owner does serve — the whole remedy. Reported as a broken request it
        // would be buried under a transport error nobody can act on.
        let owner = spawn_owner(
            400,
            r#"{"error": "unknown model 'forest:deep'; available: ('ridge:none', 'kernel:zz')"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(
            &client,
            Command::RunPredictor {
                model: "forest:deep".into(),
                offline: false,
            },
        )
        .await
        {
            Some(Wrote::PredictorRefused { said }) => {
                assert!(said.contains("available: ('ridge:none'"), "{said}");
            }
            other => panic!("a 400 from this route is a refusal: {other:?}"),
        }
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&owner.only().body).unwrap()["offline"],
            serde_json::json!(false),
            "the window's own lane must travel"
        );
    }

    #[tokio::test]
    async fn a_two_hundred_that_names_no_run_or_no_lanes_is_refused_not_rendered() {
        // The shape this reader exists for. Defaulted, a 200 without these
        // decodes to "no run · 0 fitted, nothing cleared admission" — a broken
        // contract, or a proxy's answer, drawn in the tone reserved for a
        // research finding. Invariant 4: refuse loudly.
        for body in [
            r#"{"models": ["kernel:zz", "ridge:none"], "champion": null}"#,
            r#"{"run_id": "9f3c1d77aa20", "champion": null}"#,
            r#"{"run_id": "9f3c1d77aa20", "models": [], "champion": null}"#,
        ] {
            let owner = spawn_owner(200, body);
            let client = WriteClient::new(&owner.base).unwrap();
            let err = client.run_predictor("kernel:zz", true).await.unwrap_err();
            let said = err.to_string();
            assert!(said.contains("unreadably"), "{body}: {said}");
            // The body itself, so whoever reads the log can see what answered.
            assert!(
                said.contains("9f3c1d77aa20") || said.contains("kernel:zz"),
                "{said}"
            );
        }

        // And a board that admitted nothing is still a *result*: a `null`
        // champion beside a real run and real lanes reads back cleanly.
        let owner = spawn_owner(
            200,
            r#"{"run_id": "9f3c1d77aa20", "models": ["kernel:zz", "ridge:none"],
                "champion": null}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match client.run_predictor("kernel:zz", true).await.unwrap() {
            Board::Ran { champion, .. } => assert_eq!(champion, None),
            other => panic!("{other:?}"),
        }
    }

    #[tokio::test]
    async fn a_predictor_run_that_never_landed_names_the_lane_it_was_about() {
        // Its own outcome variant, carrying the lane. A board runs for up to a
        // minute, so the pane waiting on one has to tell its own broken
        // request from every other write's — see `bus::Wrote::PredictorFailed`.
        let client = WriteClient::new("http://127.0.0.1:1").unwrap();
        match perform(
            &client,
            Command::RunPredictor {
                model: "kernel:zz".into(),
                offline: true,
            },
        )
        .await
        {
            Some(Wrote::PredictorFailed { lane, said }) => {
                assert_eq!(lane, "kernel:zz");
                assert!(!said.is_empty(), "a failure must carry the reason");
            }
            other => panic!("an unreachable owner is a predictor failure: {other:?}"),
        }
    }

    #[tokio::test]
    async fn a_question_carries_the_owners_own_answer_about_whether_it_can_be_heard() {
        // `atlas_message` answers **200** whether or not a coordinator exists to
        // read the question — "coordinator unavailable; Atlas is degraded and
        // cannot answer" is a 200 with `received: true`. A seam that reported
        // the status code would tell an operator their question was asked of
        // something that cannot hear it, which is the same class of failure as
        // reading a 200-shaped execution refusal as a fill.
        let owner = spawn_owner(
            200,
            r#"{"received": true, "coordinator_available": false,
                "note": "coordinator unavailable; Atlas is degraded and cannot answer"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::Message("why are we flat?".into())).await {
            Some(Wrote::Asked { note }) => assert!(note.contains("degraded"), "{note}"),
            other => panic!("a question must carry the owner's note: {other:?}"),
        }
        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/atlas/message");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
            serde_json::json!({"text": "why are we flat?"})
        );
    }

    #[tokio::test]
    async fn starting_a_run_names_the_template_and_reports_the_owners_own_handle() {
        let owner = spawn_owner(
            200,
            r#"{"workflow_id": "805e0729cfec4d67", "kind": "regime_review", "status": "running"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(
                &client,
                Command::StartWorkflow {
                    template: "regime_review".into(),
                    goal: "check the drift".into(),
                }
            )
            .await,
            Some(Wrote::Started {
                template: "regime_review".into(),
                workflow_id: "805e0729cfec4d67".into(),
            })
        );
        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/workflows/start");
        // `kind` and `goal` only. The owner reads `as_of`, `universe` and
        // `offline` too and defaults all three, and it refuses to take a phase
        // graph from a network caller at all — sending less is the narrower
        // surface, and the picker says so on screen.
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
            serde_json::json!({"kind": "regime_review", "goal": "check the drift"})
        );
    }

    #[tokio::test]
    async fn a_start_the_owner_did_not_hand_back_a_handle_for_is_a_failure() {
        // The owner answers with the workflow row it created. A 200 without a
        // `workflow_id` is a broken contract, and inventing a handle would put
        // a run on screen that an operator could never find in the registry —
        // the owner's own dispatch path refuses the same shape for the same
        // reason ("returning a handle with workflow_id=None is how a failed
        // dispatch used to be recorded as a completed task").
        let owner = spawn_owner(200, r#"{"status": "running"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(
            &client,
            Command::StartWorkflow {
                template: "regime_review".into(),
                goal: "check the drift".into(),
            },
        )
        .await
        {
            Some(Wrote::Failed { what, said }) => {
                assert!(what.contains("regime_review"), "{what}");
                assert!(said.contains("without a workflow_id"), "{said}");
            }
            other => panic!("a start with no handle must not read as started: {other:?}"),
        }
    }

    #[tokio::test]
    async fn the_three_commands_the_runtime_handles_itself_send_nothing() {
        // A stray `Quit` reaching the writer must not put a meaningless row on
        // the bus — every `Wrote` raises a toast and refetches the desk. The
        // catalog fetch is here for a second reason: it is a *read*, served by
        // the poller, and a write outcome for it would announce a request that
        // changed nothing.
        let client = WriteClient::new("http://127.0.0.1:1").unwrap();
        assert_eq!(perform(&client, Command::Quit).await, None);
        assert_eq!(perform(&client, Command::Refresh).await, None);
        assert_eq!(perform(&client, Command::Backends).await, None);
    }

    // -- the approval ------------------------------------------------------
    //
    // The one write this stream adds. Every test below is about the same
    // failure the execution gate shipped once: a 200 whose body says the write
    // did not happen, reported as the write happening.

    /// The task id off the fixture's own would-do block — an id the owner
    /// served, never one composed here.
    const APPROVED_TASK: &str = "9f2c1ab4d8e35007";

    #[tokio::test]
    async fn approving_a_proposal_starts_the_task_the_owner_bound_to_it() {
        // The route, the body, and the answer. `template_id` and
        // `workflow_id` are the owner's own words for what it started; a
        // client echoing the id it sent would report the request rather than
        // the answer.
        let owner = spawn_owner(
            200,
            r#"{"started": true, "completed": false, "dispatched": true,
                "workflow_id": "805e0729cfec4d67", "template_id": "regime_review"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, Command::ApproveAction(APPROVED_TASK.into())).await,
            Some(Wrote::ProposalStarted {
                task_id: APPROVED_TASK.into(),
                template: Some("regime_review".into()),
                workflow_id: Some("805e0729cfec4d67".into()),
            })
        );
        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, format!("/api/atlas/tasks/{APPROVED_TASK}/start"));
        // Nothing in the body. The owner reads `offline` off the query and
        // takes no other input on this route, and a client that sent a
        // template id would be offering the owner a second opinion about what
        // the task it already holds is for.
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
            serde_json::json!({})
        );
    }

    #[tokio::test]
    async fn a_two_hundred_that_says_it_did_not_start_is_a_refusal_and_never_a_start() {
        // The trap, on a second route. `start_task` refuses the mode gate with
        // HTTP 200 and `started: false` (`atlas.py:441`), so a client keying
        // success off the status code reports the gate's own no as work in
        // flight — and the pipeline pane then waits on a run nobody began.
        let owner = spawn_owner(
            200,
            r#"{"started": false, "blocked_by": "authority",
                "reason": "desk_rebalance_review creates a paper plan, which requires Propose mode; Atlas is in Research"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::ApproveAction(APPROVED_TASK.into())).await {
            Some(Wrote::ProposalRefused {
                task_id,
                blocked_by,
                reason,
            }) => {
                assert_eq!(task_id, APPROVED_TASK);
                assert_eq!(blocked_by, "authority");
                assert!(reason.contains("Propose mode"), "{reason}");
            }
            other => panic!("a refusal must not read as a start: {other:?}"),
        }
    }

    #[tokio::test]
    async fn a_refusal_with_no_sentence_still_says_what_stopped_it() {
        // The retry budget refuses with `blocked_by` and nothing else
        // (`atlas.py:425`). "Refused" with an empty line under it is not
        // something an operator can act on, so the gate's own word for it is
        // the reason of last resort — the same rule `Execution::read` keeps.
        let owner = spawn_owner(200, r#"{"started": false, "blocked_by": "retry_budget"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::ApproveAction(APPROVED_TASK.into())).await {
            Some(Wrote::ProposalRefused {
                blocked_by, reason, ..
            }) => {
                assert_eq!(blocked_by, "retry_budget");
                assert!(reason.contains("retry_budget"), "{reason}");
            }
            other => panic!("{other:?}"),
        }
    }

    #[tokio::test]
    async fn a_task_that_is_no_longer_queued_is_the_gate_speaking_not_a_broken_request() {
        // The one refusal this route carries a status on: `PermissionError`
        // becomes a 400 (`server.py:4486`). It is the same gate about the same
        // request, and rendering it as "write failed" would bury the sentence
        // that says today's proposal is already spent.
        let owner = spawn_owner(
            400,
            r#"{"error": "task '9f2c1ab4d8e35007' is 'completed'; only a queued or failed task may start"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::ApproveAction(APPROVED_TASK.into())).await {
            Some(Wrote::ProposalRefused { reason, .. }) => {
                assert!(reason.contains("only a queued or failed"), "{reason}")
            }
            other => panic!("a considered refusal must not read as a broken write: {other:?}"),
        }
    }

    #[tokio::test]
    async fn a_start_that_failed_inside_its_own_two_hundred_is_not_reported_as_started() {
        // `{"started": true, "completed": false, "error": …}` — the runner
        // raised inside the request. The work is already over, so a box saying
        // it started would leave an operator watching a pipeline that will
        // never move.
        let owner = spawn_owner(
            200,
            r#"{"started": true, "completed": false,
                "error": "no workflow could be started for template 'regime_review'"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::ApproveAction(APPROVED_TASK.into())).await {
            Some(Wrote::Failed { what, said }) => {
                assert!(what.contains(APPROVED_TASK), "{what}");
                assert!(said.contains("no workflow could be started"), "{said}");
            }
            other => panic!("a start that failed must not read as a start: {other:?}"),
        }
    }

    #[tokio::test]
    async fn a_start_the_owner_did_not_rule_on_is_a_failure_rather_than_a_guess() {
        // The owner sets `started` on every answer this route gives, so a body
        // without it is a broken contract — and both guesses are indefensible:
        // one reports work nobody started, the other hides work that is now
        // running.
        let owner = spawn_owner(200, r#"{"ok": true}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::ApproveAction(APPROVED_TASK.into())).await {
            Some(Wrote::Failed { said, .. }) => {
                assert!(said.contains("without saying whether it started"), "{said}")
            }
            other => panic!("{other:?}"),
        }
    }

    // -- the ask -----------------------------------------------------------
    //
    // The write that makes every test above reachable on a real desk. The
    // owner mints a proposal per startable template here and nowhere else, so
    // without this call the panel is empty and `/do` has nothing to name.

    #[tokio::test]
    async fn asking_the_desk_posts_to_the_actionables_route_and_counts_the_answer() {
        let owner = spawn_owner(
            200,
            r#"{"trading_date": "2026-08-07", "items": [
                {"template_id": "desk_brief", "startable": true, "reason": null,
                 "task_id": "9f2c1ab4d8e35007", "task_status": "queued"},
                {"template_id": "regime_review", "startable": true, "reason": null,
                 "task_id": "8f21a0c4de3b1157", "task_status": "queued"},
                {"template_id": "desk_rebalance_review", "startable": false,
                 "reason": "desk_rebalance_review creates a paper plan, which requires Propose mode; Atlas is in Research",
                 "task_id": null, "task_status": null}]}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, Command::Actionables).await,
            Some(Wrote::Proposed {
                offered: 2,
                refused: 1
            })
        );
        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/atlas/actionables");
        // Nothing in the body: the owner reads `offline` off the query and
        // composes the menu from its own facts. A client that sent a mode or a
        // template list would be offering the gate a second opinion.
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
            serde_json::json!({})
        );
    }

    #[tokio::test]
    async fn an_ask_that_offers_nothing_is_still_an_answer_and_says_so() {
        // Observe refuses every template. The desk answered the question — the
        // answer is "nothing" — so this is not a failure, and it must not read
        // as one: the refusals are on the panel with their reasons.
        let owner = spawn_owner(
            200,
            r#"{"trading_date": "2026-08-07", "items": [
                {"template_id": "desk_brief", "startable": false,
                 "reason": "'desk_brief' requires Research mode; Atlas is in Observe mode",
                 "task_id": null, "task_status": null}]}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, Command::Actionables).await,
            Some(Wrote::Proposed {
                offered: 0,
                refused: 1
            })
        );
    }

    #[tokio::test]
    async fn an_ask_the_owner_answered_without_a_verdict_is_a_broken_contract() {
        // The POST is where the gate speaks, so `startable` is a boolean on
        // every item it serves — `null` is the snapshot's "not ruled on here".
        // Counting an unruled item as either would put a number on screen the
        // desk never said, and the offered half is the one an operator acts on.
        let owner = spawn_owner(
            200,
            r#"{"items": [{"template_id": "desk_brief", "startable": null}]}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::Actionables).await {
            Some(Wrote::Failed { what, said }) => {
                assert!(what.contains("would do"), "{what}");
                assert!(said.contains("whether it may start"), "{said}");
            }
            other => panic!("an unreadable answer must not read as an ask: {other:?}"),
        }
        // And a 200 with no list at all is the same answer.
        let owner = spawn_owner(200, r#"{"trading_date": "2026-08-07"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::Actionables).await {
            Some(Wrote::Failed { said, .. }) => {
                assert!(said.contains("without a list of actionables"), "{said}")
            }
            other => panic!("{other:?}"),
        }
    }

    #[tokio::test]
    async fn an_unarmed_window_cannot_ask_because_asking_writes_to_the_queue() {
        // The scope's posture filter refuses this line in `resolve`; this is
        // the gate in series behind it. An ask mints `proposal`-origin task
        // rows, so a read-only window may look at a panel somebody else filled
        // and may not fill one.
        let owner = spawn_owner(200, r#"{"items": []}"#);
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let writes = Writes::new(&owner.base, false, tx).unwrap();
        writes.dispatch(Command::Actionables, Posture::Glass);
        match rx.recv().await {
            Some(AppEvent::Wrote(Wrote::Failed { what, said })) => {
                assert!(what.contains("would do"), "{what}");
                assert!(said.contains("not armed"), "{said}");
            }
            other => panic!(
                "an unarmed ask must fail loudly: {}",
                other.as_ref().map_or("nothing at all", debug)
            ),
        }
        assert!(
            owner.seen.lock().unwrap().is_empty(),
            "an unarmed window reached the owner"
        );
    }

    #[tokio::test]
    async fn an_unarmed_window_cannot_approve_a_proposal_at_the_chokepoint_either() {
        // The scope's posture filter refuses this line in `resolve`; this is
        // the gate in series behind it. A desk disarmed between the frame that
        // drew the panel and the Enter that approved an item must not write.
        let owner = spawn_owner(200, r#"{"started": true}"#);
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let writes = Writes::new(&owner.base, false, tx).unwrap();
        writes.dispatch(Command::ApproveAction(APPROVED_TASK.into()), Posture::Glass);
        match rx.recv().await {
            Some(AppEvent::Wrote(Wrote::Failed { what, said })) => {
                assert!(what.contains(APPROVED_TASK), "{what}");
                assert!(said.contains("not armed"), "{said}");
            }
            other => panic!(
                "an unarmed approval must fail loudly: {}",
                other.as_ref().map_or("nothing at all", debug)
            ),
        }
        assert!(
            owner.seen.lock().unwrap().is_empty(),
            "an unarmed window reached the owner"
        );
    }

    #[tokio::test]
    async fn a_dispatched_command_puts_its_outcome_on_the_bus() {
        // The other half of the seam: the runtime never awaits a write, so an
        // outcome that never reached the channel would be a key an operator
        // pressed and heard nothing back from.
        let owner = spawn_owner(200, r#"{"status": "approved"}"#);
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let writes = Writes::new(&owner.base, false, tx).unwrap();
        assert!(writes.armed());
        writes.dispatch(
            Command::Approve("1a2b3c4d5e6f7081".into()),
            Posture::Operator,
        );
        match rx.recv().await {
            Some(AppEvent::Wrote(Wrote::Decided { decision, .. })) => {
                assert_eq!(decision, "approved")
            }
            other => panic!("the outcome never reached the bus: {:?}", other.is_some()),
        }
    }

    #[tokio::test]
    async fn a_window_the_operator_vetoed_holds_no_writer_and_sends_nothing() {
        // `--glass` is the operator declining an authority the desk may be
        // offering, and the runtime has to agree with the status line rather
        // than merely the renderer: a vetoed window holds no client at all.
        let owner = spawn_owner(200, r#"{"status": "approved"}"#);
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let writes = Writes::new(&owner.base, true, tx).unwrap();
        assert!(!writes.armed());
        // Posture::Operator on purpose: this pins the *writer* gate, not the
        // posture gate above it. A vetoed window that somehow derived a writing
        // posture must still reach nobody, and must say so.
        writes.dispatch(
            Command::Approve("1a2b3c4d5e6f7081".into()),
            Posture::Operator,
        );
        tokio::time::sleep(std::time::Duration::from_millis(150)).await;
        match rx.try_recv() {
            Ok(AppEvent::Wrote(Wrote::Failed { what, said })) => {
                assert_eq!(what, "approve 1a2b3c4d5e6f7081");
                assert!(said.contains("--glass"), "{said}");
            }
            Ok(other) => panic!("a vetoed window sent something else: {}", debug(&other)),
            Err(err) => panic!("a vetoed window was silently dropped: {err}"),
        }
        assert!(
            owner.seen.lock().unwrap().is_empty(),
            "a vetoed window reached the owner"
        );
    }

    #[tokio::test]
    async fn an_unarmed_posture_is_refused_at_the_dispatch_seam_and_never_reaches_the_owner() {
        // The chokepoint, and the reason it exists one level above the views:
        // this window holds a live writer — it was not started with `--glass` —
        // and the *only* thing between the command and the owner is the posture
        // the desk last reported. Both sides of that guard are witnessed here
        // and in `a_dispatched_command_puts_its_outcome_on_the_bus` above.
        let owner = spawn_owner(200, r#"{"status": "approved"}"#);
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let writes = Writes::new(&owner.base, false, tx).unwrap();
        assert!(writes.armed(), "the writer exists; the posture is the gate");
        writes.dispatch(Command::Approve("1a2b3c4d5e6f7081".into()), Posture::Glass);
        tokio::time::sleep(std::time::Duration::from_millis(150)).await;
        // Loud, not dropped: a refusal on the bus is what puts a toast in front
        // of the operator, and a key that silently did nothing is the failure
        // mode invariant 4 exists for.
        match rx.try_recv() {
            Ok(AppEvent::Wrote(Wrote::Failed { what, said })) => {
                assert_eq!(what, "approve 1a2b3c4d5e6f7081");
                assert!(said.contains("not armed"), "{said}");
            }
            Ok(other) => panic!("an unarmed window sent something else: {}", debug(&other)),
            Err(err) => panic!("an unarmed window was silently dropped: {err}"),
        }
        assert!(
            owner.seen.lock().unwrap().is_empty(),
            "an unarmed window reached the owner"
        );
    }

    #[tokio::test]
    async fn the_arming_answer_is_the_one_write_an_unarmed_window_may_make() {
        // The exception the gate above has to carry, and the reason it is not
        // a second dispatch path: every window that can arm a desk is, by
        // definition, one the desk has not armed yet, so a chokepoint with no
        // exemption would make the arming question unanswerable. It grants
        // nothing on its own — the owner records a posture and the *next*
        // snapshot is what widens this window.
        let owner = spawn_owner(200, r#"{"armed": true, "chosen": true}"#);
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let writes = Writes::new(&owner.base, false, tx).unwrap();
        writes.dispatch(Command::Posture { armed: true }, Posture::Glass);
        match rx.recv().await {
            Some(AppEvent::Wrote(Wrote::Armed { armed })) => assert!(armed),
            other => panic!("the arming answer never landed: {:?}", other.is_some()),
        }
        let seen = owner.only();
        assert_eq!(seen.path, "/api/desk/posture");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
            serde_json::json!({"armed": true})
        );
    }

    #[tokio::test]
    async fn a_window_the_operator_vetoed_cannot_arm_the_desk_either() {
        // The gate below the exemption, which the exemption must not step
        // past: `--glass` holds no writer, and a window that vetoed its own
        // authority may not vote itself back into it.
        let owner = spawn_owner(200, r#"{"armed": true, "chosen": true}"#);
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let writes = Writes::new(&owner.base, true, tx).unwrap();
        writes.dispatch(Command::Posture { armed: true }, Posture::Glass);
        tokio::time::sleep(std::time::Duration::from_millis(150)).await;
        match rx.try_recv() {
            Ok(AppEvent::Wrote(Wrote::Failed { what, said })) => {
                assert_eq!(what, "arm this desk");
                assert!(said.contains("--glass"), "{said}");
            }
            Ok(other) => panic!("a vetoed window sent something else: {}", debug(&other)),
            Err(err) => panic!("a vetoed window was silently dropped: {err}"),
        }
        assert!(
            owner.seen.lock().unwrap().is_empty(),
            "a vetoed window reached the owner"
        );
    }

    #[tokio::test]
    async fn an_owner_that_does_not_say_what_it_armed_is_a_failure_and_not_a_receipt() {
        // `set_posture` answers with `posture_payload()`, so a 200 without
        // `armed` is a broken contract — and this client will not report an
        // arming it cannot read back, any more than it invents a desk label.
        let owner = spawn_owner(200, r#"{"chosen": true}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::Posture { armed: true }).await {
            Some(Wrote::Failed { what, said }) => {
                assert_eq!(what, "arm this desk");
                assert!(said.contains("armed"), "{said}");
            }
            other => panic!("an unreadable answer became {other:?}"),
        }
    }

    #[tokio::test]
    async fn a_desk_disarmed_while_the_confirm_modal_was_open_books_nothing() {
        // The mid-session flip. The view gated `x` on the posture when the modal
        // was *opened*; the keystroke that mints the token and emits
        // `Command::Execute` is a different keystroke, and by then the desk may
        // have been disarmed from another window. The token is real — minted by
        // the real modal against the owner's own hash — so what refuses this is
        // the seam and nothing else.
        let token = armed_token();

        let owner = spawn_owner(200, r#"{"executed": true}"#);
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let writes = Writes::new(&owner.base, false, tx).unwrap();
        // The desk went read-only between the open and the last keystroke.
        writes.dispatch(Command::Execute(token), Posture::Glass);
        tokio::time::sleep(std::time::Duration::from_millis(150)).await;
        match rx.try_recv() {
            Ok(AppEvent::Wrote(Wrote::Failed { what, said })) => {
                assert!(what.starts_with("execute "), "{what}");
                assert!(said.contains("not armed"), "{said}");
            }
            Ok(other) => panic!("a disarmed desk sent something else: {}", debug(&other)),
            Err(err) => panic!("a disarmed desk was silently dropped: {err}"),
        }
        assert!(
            owner.seen.lock().unwrap().is_empty(),
            "a disarmed desk reached the owner"
        );
    }

    #[test]
    fn every_write_outcome_brings_the_next_poll_forward_failures_included() {
        // The rule this pins is the counter-intuitive one. A refusal moved the
        // registry — the gate invalidates the approval it declined — and a
        // *failure* is the outcome where the desk's state is least knowable,
        // because the write shares the poller's timeout and a request that gave
        // up may still be booking. Suppressing the refetch there kept the least
        // trustworthy frame on screen at the moment an operator is most likely
        // to press the key again.
        for outcome in [
            Wrote::Executed {
                plan_id: "p1".into(),
            },
            Wrote::Refused {
                plan_id: "p1".into(),
                blocked_by: "approval".into(),
                reasons: vec!["expired".into()],
            },
            Wrote::Decided {
                approval_id: "a1".into(),
                decision: "approved",
            },
            Wrote::Failed {
                what: "execute p1".into(),
                said: "the owner did not answer".into(),
            },
            // Both of the workforce verbs move the registry too: a message is
            // recorded as an audit event, and a start writes a workflow and its
            // whole phase graph. A frame that waited out the poll interval
            // would show neither, which reads as a key that did nothing.
            Wrote::Asked {
                note: "queued for the interpreting agent".into(),
            },
            Wrote::Started {
                template: "regime_review".into(),
                workflow_id: "805e0729cfec4d67".into(),
            },
            // A model choice is persisted by the owner and served back in the
            // next snapshot's `llm` block, which is what SETTINGS' MODELS card
            // draws. Without the refetch that card would keep naming the old
            // backend for a whole poll interval after the operator moved it.
            Wrote::Chose {
                said: "Atlas reasons with ollama qwen2.5:7b".into(),
            },
            // And the refusal, for the reason the failures are here: nothing
            // moved, but the reading the operator is about to act on is the one
            // on screen, and the catalog behind the refusal is what changed
            // under it.
            Wrote::ChoiceRefused {
                said: "ollama is not running at http://127.0.0.1:11499".into(),
            },
        ] {
            assert!(
                refetches(&AppEvent::Wrote(outcome.clone())),
                "{outcome:?} must bring the next poll forward"
            );
        }
    }

    // -- the model choice --------------------------------------------------
    //
    // Bodies and outcomes pinned against `POST /api/llm` as the owner actually
    // answers it — every sentence below was captured from a live worktree owner
    // rather than copied from a note that can rot.

    #[tokio::test]
    async fn a_model_choice_lands_on_the_route_in_the_two_shapes_the_owner_takes() {
        // The pair and the switch are one route with two bodies, and the
        // difference is load-bearing: an absent `backend`/`model` means "leave
        // the pair alone", which is what makes `{surface, enabled}` a switch. A
        // client that sent the current pair alongside `enabled: false` would
        // re-validate a daemon that is probably the reason it was sent.
        let owner = spawn_owner(
            200,
            r#"{"surface": "reasoner", "reasoner": {"backend": "ollama", "model": "qwen2.5:7b"},
                "workforce": {"backend": "claude", "model": "inherit"},
                "reasoner_enabled": false,
                "effect": "Atlas answers you on ollama qwen2.5:7b; enable the reasoner to let it choose templates too"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let said = match client
            .set_llm("reasoner", Some(("ollama", "qwen2.5:7b")), None)
            .await
            .unwrap()
        {
            Choice::Chosen(said) => said,
            other => panic!("{other:?}"),
        };
        // The owner's own account of what it did, which is not what an operator
        // would assume: naming a reasoner model does not switch the reasoner on,
        // and only this sentence says so.
        assert!(said.contains("enable the reasoner"), "{said}");
        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/llm");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
            serde_json::json!({"surface": "reasoner", "backend": "ollama",
                               "model": "qwen2.5:7b"}),
            "a pair choice carries no `enabled` nobody asked for"
        );

        let owner = spawn_owner(
            200,
            r#"{"surface": "reasoner", "reasoner": {"backend": "ollama", "model": "qwen2.5:7b"},
                "workforce": {"backend": "claude", "model": "inherit"},
                "reasoner_enabled": true, "effect": "Atlas reasons with ollama qwen2.5:7b"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert!(matches!(
            client.set_llm("reasoner", None, Some(true)).await.unwrap(),
            Choice::Chosen(_)
        ));
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&owner.only().body).unwrap(),
            serde_json::json!({"surface": "reasoner", "enabled": true}),
            "the switch names no pair, which is what leaves the pair alone"
        );
    }

    #[tokio::test]
    async fn every_way_the_owner_says_no_to_a_model_is_a_refusal_and_not_a_failure() {
        // All four are 400s with a sentence written for a human, and all four
        // are considered answers to a well-formed request. Folded into `Err`
        // they would arrive as "the owner refused with 400: {…}" — the remedy
        // buried inside a transport error nobody can act on.
        for said in [
            "ollama is not running at http://127.0.0.1:11499 — start it with `ollama serve`",
            "the ollama backend cannot serve 'granite3.3:8b' right now; it serves qwen2.5:7b",
            "only the reasoner surface can be switched on or off",
            "unknown model surface 'banana'; the desk has reasoner and workforce",
        ] {
            let owner = spawn_owner(400, serde_json::json!({"error": said}).to_string());
            let client = WriteClient::new(&owner.base).unwrap();
            match client
                .set_llm("workforce", Some(("ollama", "granite3.3:8b")), None)
                .await
            {
                Ok(Choice::Rejected(back)) => assert_eq!(back, said),
                other => panic!("{said}: {other:?}"),
            }
        }
    }

    #[tokio::test]
    async fn a_model_answer_the_owner_did_not_explain_is_a_failure_and_a_broken_owner_is_an_error()
    {
        // `set_llm_config` always returns its `effect`. A 200 without one is a
        // broken contract, and inventing a receipt out of the two words just
        // sent is the shape `desk_mode`'s label and `start_workflow`'s handle
        // already refuse.
        for body in [
            r#"{"surface": "reasoner", "reasoner_enabled": true}"#,
            r#"{"surface": "reasoner", "effect": ""}"#,
        ] {
            let owner = spawn_owner(200, body);
            let client = WriteClient::new(&owner.base).unwrap();
            match client.set_llm("reasoner", None, Some(true)).await {
                Err(err) => assert!(
                    err.to_string().contains("without saying what it did"),
                    "{err}"
                ),
                other => panic!("{body}: {other:?}"),
            }
        }
        // And a status that is not a considered answer stays an error: a 502 is
        // something in front of the desk, not the desk saying no, and offering
        // it as a refusal would put a proxy's page where the owner's remedy goes.
        let owner = spawn_owner(502, "<html>bad gateway</html>");
        let client = WriteClient::new(&owner.base).unwrap();
        assert!(client.set_llm("reasoner", None, Some(false)).await.is_err());
    }

    #[tokio::test]
    async fn a_model_command_reaches_the_owner_and_comes_back_in_its_own_words() {
        // The dispatch routing: which method a `Command` lands on, and which
        // `Wrote` each answer becomes.
        let owner = spawn_owner(
            200,
            r#"{"surface": "workforce", "effect": "the workforce roles run on claude inherit"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(
                &client,
                Command::SetLlm {
                    surface: "workforce".into(),
                    choice: atlas::cmd::ModelChoice::Pair {
                        backend: "claude".into(),
                        model: "inherit".into(),
                    },
                }
            )
            .await,
            Some(Wrote::Chose {
                said: "the workforce roles run on claude inherit".into(),
            })
        );
        assert_eq!(owner.only().path, "/api/llm");

        let owner = spawn_owner(
            400,
            r#"{"error": "only the reasoner surface can be switched on or off"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let outcome = perform(
            &client,
            Command::SetLlm {
                surface: "workforce".into(),
                choice: atlas::cmd::ModelChoice::Enabled(false),
            },
        )
        .await;
        assert_eq!(
            outcome,
            Some(Wrote::ChoiceRefused {
                said: "only the reasoner surface can be switched on or off".into(),
            })
        );
        // And both reach the operator. A `Wrote` variant with no toast arm is a
        // write nobody is told about; the refusal is `Warn` because the desk
        // considered it and nothing moved.
        let toast = atlas::ui::widgets::toast::for_event(&AppEvent::Wrote(outcome.unwrap()))
            .expect("a refused choice owes the operator a box");
        assert_eq!(toast.level, atlas::ui::widgets::toast::Level::Warn);
        assert!(toast.message.contains("only the reasoner"), "{toast:?}");

        // A request that never landed names the action it was about — and names
        // the switch as a switch, not as a pair it never sent.
        let client = WriteClient::new("http://127.0.0.1:1").unwrap();
        match perform(
            &client,
            Command::SetLlm {
                surface: "reasoner".into(),
                choice: atlas::cmd::ModelChoice::Enabled(false),
            },
        )
        .await
        {
            Some(Wrote::Failed { what, said }) => {
                assert_eq!(what, "switch the reasoner off");
                assert!(!said.is_empty());
            }
            other => panic!("{other:?}"),
        }
    }

    // -- the desk mode's answer -------------------------------------------
    //
    // The request half of `/mode` is pinned above with every other verb. This
    // is what the owner says *back*, which is where the two ways this command
    // can mislead an operator live: a switch reported off a body that never
    // named the desk, and a real book accepted with a login that does not work.

    /// The owner's own answer shape (`desk_mode_payload`), with the parts a
    /// test varies spelled out.
    fn desk_mode_body(label: &str, credentials: &str) -> String {
        format!(
            r#"{{"data": "live", "book": "alpaca", "label": "{label}",
                 "offline": false, {credentials}}}"#
        )
    }

    fn point_at(book: &str) -> Command {
        Command::DeskMode {
            data: "live".into(),
            book: book.into(),
        }
    }

    #[tokio::test]
    async fn a_desk_mode_reports_the_owners_own_label_and_reaches_the_screen() {
        // The label is the sentence the *owner* makes of the pair. This client
        // composing one out of the two words it just sent would be a receipt of
        // its own making — the same shape as reporting a 200-with-`executed:
        // false` as a fill, one authority down.
        let owner = spawn_owner(
            200,
            r#"{"data": "live", "book": "simulated", "label": "LIVE · SIM BOOK",
                "offline": false, "credentials_ok": true, "credentials": "none needed"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let outcome = perform(&client, point_at("simulated")).await;
        assert_eq!(
            outcome,
            Some(Wrote::Pointed {
                label: "LIVE · SIM BOOK".into(),
                warning: None,
            })
        );
        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/desk_mode");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
            serde_json::json!({"data": "live", "book": "simulated"})
        );

        // And it reaches the operator, in the owner's words. A `Wrote` variant
        // with no toast arm is a write nobody is told about.
        let toast = atlas::ui::widgets::toast::for_event(&AppEvent::Wrote(outcome.unwrap()))
            .expect("a desk mode change owes the operator a box");
        assert_eq!(toast.level, atlas::ui::widgets::toast::Level::Info);
        assert!(toast.message.contains("LIVE · SIM BOOK"), "{toast:?}");
    }

    #[tokio::test]
    async fn a_desk_mode_the_owner_did_not_name_is_a_failure() {
        // `desk_mode_payload` always carries a label. A 200 without one is a
        // broken contract, and the precedent is `StartWorkflow`: a handle the
        // owner did not hand back is not invented either.
        for body in [
            r#"{"data": "live", "book": "alpaca", "offline": false}"#,
            r#"{"data": "live", "book": "alpaca", "label": "", "offline": false}"#,
        ] {
            let owner = spawn_owner(200, body);
            let client = WriteClient::new(&owner.base).unwrap();
            match perform(&client, point_at("alpaca")).await {
                Some(Wrote::Failed { what, said }) => {
                    assert!(what.contains("alpaca"), "{what}");
                    assert!(said.contains("without a label"), "{said}");
                }
                other => panic!("an unlabelled 200 must not read as a switch: {other:?}"),
            }
        }
    }

    #[tokio::test]
    async fn a_real_book_the_owner_cannot_reach_warns_instead_of_reading_as_a_switch() {
        // A 200 that changed the desk and cannot trade it. Reporting that as a
        // clean switch is the "succeeded and did nothing" shape this client
        // refuses everywhere else.
        let owner = spawn_owner(
            200,
            desk_mode_body(
                "LIVE · ALPACA BOOK",
                r#""credentials_ok": false, "credentials": "ALPACA_API_KEY_ID is not set""#,
            ),
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let outcome = perform(&client, point_at("alpaca")).await;
        match &outcome {
            Some(Wrote::Pointed { label, warning }) => {
                assert_eq!(label, "LIVE · ALPACA BOOK");
                assert_eq!(warning.as_deref(), Some("ALPACA_API_KEY_ID is not set"));
            }
            other => panic!("{other:?}"),
        }
        let toast = atlas::ui::widgets::toast::for_event(&AppEvent::Wrote(outcome.unwrap()))
            .expect("a box");
        assert_eq!(
            toast.level,
            atlas::ui::widgets::toast::Level::Warn,
            "a book that cannot trade must not be drawn as a clean switch"
        );
        assert!(toast.message.contains("ALPACA_API_KEY_ID"), "{toast:?}");
    }

    #[tokio::test]
    async fn the_door_can_take_a_book_it_cannot_reach_and_the_warning_rides_its_own_outcome() {
        // The startup door may now point a desk at the real book with no login
        // behind it — the gate that used to refuse that was authority the door
        // never had, and refusing it also removed the one walk that ends at the
        // login form. The honesty it traded for is *this*: the door's outcome
        // is the same `Command::DeskMode` every other switch produces, so the
        // credential warning the test above pins rides it too.
        //
        // Driven through the real router rather than hand-built, because the
        // claim being checked is that the door's command **is** that command.
        let mut store = atlas::store::Store::default();
        store.apply(
            atlas::bus::AppEvent::Snapshot(Box::new(
                serde_json::from_value(serde_json::json!({
                    // The desk arms this window, not the test: the posture is
                    // re-derived from every snapshot.
                    "posture": {"armed": true, "chosen": true},
                    "desk_mode": {"data": "synthetic", "book": "simulated",
                                  "label": "SYNTHETIC", "offline": true,
                                  "credentials": "no ALPACA_API_KEY_ID in the environment",
                                  "credentials_ok": false}
                }))
                .unwrap(),
            )),
            std::time::Instant::now(),
        );
        store.pick();
        let mut views = atlas::ui::views::Views::new();
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
        // The runtime draws before its first event, and the door reads its
        // floor off the frame it was last given.
        frame_with(&store, &views);
        // LIVE, then the book the desk cannot reach, then on to the models,
        // then keep them: the walk the ruling makes reachable.
        let mut acted = None;
        for code in [
            KeyCode::Down,
            KeyCode::Enter,
            KeyCode::Down,
            KeyCode::Down,
            KeyCode::Enter,
            KeyCode::Down,
            KeyCode::Enter,
            KeyCode::Enter,
        ] {
            acted = atlas::ui::shell::on_key(
                KeyEvent::new(code, KeyModifiers::NONE),
                &mut store,
                &mut views,
            );
            frame_with(&store, &views);
        }
        let cmd = acted.expect("the walk ends by applying the pair");
        assert_eq!(
            cmd,
            Command::DeskMode {
                data: "live".into(),
                book: "alpaca".into()
            }
        );
        // And it lands the operator in front of the login, which is the other
        // half of what the gate's removal bought.
        assert_eq!(store.nav.view, atlas::store::ViewId::Settings);

        let owner = spawn_owner(
            200,
            desk_mode_body(
                "LIVE · ALPACA BOOK",
                r#""credentials_ok": false, "credentials": "ALPACA_API_KEY_ID is not set""#,
            ),
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let outcome = perform(&client, cmd).await;
        match &outcome {
            Some(Wrote::Pointed { warning, .. }) => assert_eq!(
                warning.as_deref(),
                Some("ALPACA_API_KEY_ID is not set"),
                "the door's own switch reported a book it cannot trade as a clean one"
            ),
            other => panic!("{other:?}"),
        }
        let toast = atlas::ui::widgets::toast::for_event(&AppEvent::Wrote(outcome.unwrap()))
            .expect("a box");
        assert_eq!(toast.level, atlas::ui::widgets::toast::Level::Warn);
        assert!(toast.message.contains("ALPACA_API_KEY_ID"), "{toast:?}");
    }

    #[tokio::test]
    async fn the_credential_warning_speaks_only_about_the_book_that_can_trade() {
        // The simulated book needs no login, so a warning beside it would train
        // an operator to read past the one that matters.
        let owner = spawn_owner(
            200,
            r#"{"data": "synthetic", "book": "simulated", "label": "SYNTHETIC",
                "offline": true, "credentials_ok": false, "credentials": "no credentials"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(
                &client,
                Command::DeskMode {
                    data: "synthetic".into(),
                    book: "simulated".into()
                }
            )
            .await,
            Some(Wrote::Pointed {
                label: "SYNTHETIC".into(),
                warning: None,
            })
        );
    }

    #[tokio::test]
    async fn a_working_alpaca_login_is_not_warned_about() {
        let owner = spawn_owner(
            200,
            desk_mode_body(
                "LIVE · ALPACA BOOK",
                r#""credentials_ok": true, "credentials": "paper key ending 4f21""#,
            ),
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, point_at("alpaca")).await,
            Some(Wrote::Pointed {
                label: "LIVE · ALPACA BOOK".into(),
                warning: None,
            })
        );
    }

    #[tokio::test]
    async fn an_owner_that_will_not_say_is_warned_about_rather_than_assumed_fine() {
        // Two shapes of silence: the flag absent, and a description absent
        // behind a false flag. Neither may pass as a working login on the book
        // that can place real orders — invariant 4, refuse loudly.
        let quiet = spawn_owner(
            200,
            r#"{"data": "live", "book": "alpaca", "label": "LIVE · ALPACA BOOK",
                "offline": false}"#,
        );
        let client = WriteClient::new(&quiet.base).unwrap();
        match perform(&client, point_at("alpaca")).await {
            Some(Wrote::Pointed { warning, .. }) => {
                assert!(warning.unwrap().contains("did not say"));
            }
            other => panic!("{other:?}"),
        }

        // Two shapes of "false with nothing said": the field missing, and the
        // field present and empty. `Some("")` is absent everywhere else in this
        // client, and a warning that renders as a dangling dash says less than
        // the fallback sentence does.
        for credentials in [
            r#""credentials_ok": false"#,
            r#""credentials_ok": false, "credentials": """#,
        ] {
            let vague = spawn_owner(200, desk_mode_body("LIVE · ALPACA BOOK", credentials));
            let client = WriteClient::new(&vague.base).unwrap();
            match perform(&client, point_at("alpaca")).await {
                Some(Wrote::Pointed { warning, .. }) => {
                    assert!(
                        warning.unwrap().contains("no usable Alpaca credentials"),
                        "{credentials}"
                    );
                }
                other => panic!("{credentials}: {other:?}"),
            }
        }
    }

    #[tokio::test]
    async fn the_pair_the_owner_forbids_comes_back_in_its_own_words() {
        // `DeskMode.__post_init__` refuses synthetic data on the Alpaca book.
        // This client does not re-check that rule — a second copy would drift
        // from the one that decides — so the owner's 400 is what an operator
        // reads, verbatim.
        let owner = spawn_owner(
            400,
            r#"{"error": "synthetic data cannot trade the Alpaca book"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(
            &client,
            Command::DeskMode {
                data: "synthetic".into(),
                book: "alpaca".into(),
            },
        )
        .await
        {
            Some(Wrote::Failed { what, said }) => {
                assert!(what.contains("synthetic"), "{what}");
                assert!(said.contains("cannot trade the Alpaca book"), "{said}");
            }
            other => panic!("a refused pair must not read as a switch: {other:?}"),
        }
    }

    // -- the alpaca login --------------------------------------------------
    //
    // C1's settled contract, driven against a canned owner rather than read off
    // its report: `POST /api/alpaca/credentials` answers 200 with
    // `desk_mode_payload()`, **400 carrying `confirm`** when a stored login
    // would be destroyed, and 400 without it when the request itself is wrong.
    // The two 400s are told apart by that field and never by the sentence — the
    // validation refusal ("replace must be true or false") contains the word.

    use atlas::net::write::{Login, WriteError};
    use atlas::secret::Secret;

    /// A plausible pair, in the shapes the owner's own patterns admit.
    fn pair() -> (Secret, Secret) {
        (
            Secret::new("PKTEST0123456789".into()),
            Secret::new("s3cret/abcdefghijklmnopqrstuv".into()),
        )
    }

    /// The owner's answer to a login it accepted.
    const STORED: &str = r#"{"data": "live", "book": "alpaca", "label": "LIVE · ALPACA BOOK",
        "offline": false, "credentials_ok": true, "credentials": "paper key ending 4f21"}"#;

    /// The consent refusal, in the owner's own words (`AlpacaConsentRequired`).
    const CONSENT: &str = r#"{"error": "the active alpaca profile holds a browser login; storing a key pair discards its access token and refresh token, and they cannot be recovered without logging in again", "confirm": "replace"}"#;

    #[tokio::test]
    async fn a_login_carries_the_pair_and_never_a_replace_nobody_asked_for() {
        let owner = spawn_owner(200, STORED);
        let client = WriteClient::new(&owner.base).unwrap();
        let (key, secret) = pair();
        let out = client
            .set_alpaca_credentials(&key, &secret, None)
            .await
            .unwrap();
        assert!(matches!(out, Login::Stored(_)), "{out:?}");

        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/alpaca/credentials");
        // `replace` is absent, not `false`: the owner defaults it, and a client
        // that sent the flag on every login would be one edit away from sending
        // `true` on every login.
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
            serde_json::json!({
                "api_key": "PKTEST0123456789",
                "api_secret": "s3cret/abcdefghijklmnopqrstuv"
            })
        );
    }

    #[tokio::test]
    async fn the_two_four_hundreds_are_told_apart_by_the_confirm_field_and_not_by_the_words() {
        // The consentable one, rendered verbatim: the sentence names what would
        // be lost, and this client owns none of that wording.
        let owner = spawn_owner(400, CONSENT);
        let client = WriteClient::new(&owner.base).unwrap();
        let (key, secret) = pair();
        match client
            .set_alpaca_credentials(&key, &secret, None)
            .await
            .unwrap()
        {
            Login::ConsentNeeded(said) => {
                assert!(said.contains("refresh token"), "{said}");
                assert!(!said.contains("confirm"), "the field is not the sentence");
            }
            other => panic!("{other:?}"),
        }

        // The trap. This refusal *contains the word* `replace` and is not
        // confirmable — a client sniffing the sentence would offer to destroy a
        // profile over a body the operator should fix instead.
        let owner = spawn_owner(400, r#"{"error": "replace must be true or false"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        match client
            .set_alpaca_credentials(&key, &secret, None)
            .await
            .unwrap()
        {
            Login::Rejected(said) => assert!(said.contains("true or false"), "{said}"),
            other => panic!("a validation refusal must not offer consent: {other:?}"),
        }

        // A field this client cannot act on is not a question either. The value
        // names *which* flag would grant the request, and there is exactly one
        // this form knows how to set — offering to confirm anything else would
        // be sending `replace: true` at a refusal that never asked for it.
        let owner = spawn_owner(
            400,
            r#"{"error": "the desk is not configured for that", "confirm": "force"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match client
            .set_alpaca_credentials(&key, &secret, None)
            .await
            .unwrap()
        {
            Login::Rejected(said) => assert!(said.contains("not configured"), "{said}"),
            other => panic!("an unknown confirm flag is not consent: {other:?}"),
        }

        // And consent, once given, is the only thing that sets the flag.
        let owner = spawn_owner(200, STORED);
        let client = WriteClient::new(&owner.base).unwrap();
        client
            .set_alpaca_credentials(&key, &secret, Some(true))
            .await
            .unwrap();
        let body: serde_json::Value = serde_json::from_str(&owner.only().body).unwrap();
        assert_eq!(body["replace"], serde_json::json!(true));
    }

    #[tokio::test]
    async fn only_a_four_hundred_can_ask_this_client_to_overwrite_a_stored_login() {
        // A 500 carrying the field is a broken owner, not an invitation to
        // discard a browser login. It stays an error, and the form never puts
        // the consent question up.
        let owner = spawn_owner(500, CONSENT);
        let client = WriteClient::new(&owner.base).unwrap();
        let (key, secret) = pair();
        match client.set_alpaca_credentials(&key, &secret, None).await {
            Err(WriteError::Refused { status, .. }) => assert_eq!(status, 500),
            other => panic!("a 500 must not become a consent prompt: {other:?}"),
        }

        // A 400 that is not JSON at all is still a refusal to fix — never a
        // consent prompt, and never an error that loses what the owner said.
        let owner = spawn_owner(400, "the desk is not answering json today");
        let client = WriteClient::new(&owner.base).unwrap();
        match client
            .set_alpaca_credentials(&key, &secret, None)
            .await
            .unwrap()
        {
            Login::Rejected(said) => assert!(said.contains("not answering json"), "{said}"),
            other => panic!("{other:?}"),
        }
    }

    /// A reply that hands back exactly what was sent — the shape an interposing
    /// proxy produces, at whichever status it likes.
    const ECHOED: &str = r#"{"error": "cannot POST api_key=PKTEST0123456789 api_secret=s3cret/abcdefghijklmnopqrstuv to upstream"}"#;

    /// What the login form's own box says after one outcome.
    ///
    /// The third surface, and the one with no length of its own: `Form::note`
    /// wraps into whatever room the box has. Driven through the real shell,
    /// because the question is what a *renderer* did with the outcome rather
    /// than what the outcome said.
    fn form_note_after(outcome: &Wrote) -> String {
        use atlas::store::{Store, ViewId};
        use atlas::ui::views::Views;
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
        let mut store = Store::default();
        store.nav.view = ViewId::Settings;
        let _ = store.apply(
            AppEvent::Snapshot(Box::new(snapshot())),
            std::time::Instant::now(),
        );
        // After the snapshot: the fixture carries no posture block, and every
        // payload re-derives the scope.
        store.posture = Posture::Operator;
        let mut views = Views::new();
        fn press(store: &mut Store, views: &mut Views, code: KeyCode) {
            atlas::ui::shell::on_key(KeyEvent::new(code, KeyModifiers::NONE), store, views);
        }
        // A frame first: the pane publishes the area the form's floor is read
        // off, exactly as the runtime draws one before its first event.
        frame_with(&store, &views);
        press(&mut store, &mut views, KeyCode::Char('a'));
        for c in "PKTEST0123456789".chars() {
            press(&mut store, &mut views, KeyCode::Char(c));
        }
        press(&mut store, &mut views, KeyCode::Tab);
        for c in "s3cret/abcdefghijklmnopqrstuv".chars() {
            press(&mut store, &mut views, KeyCode::Char(c));
        }
        press(&mut store, &mut views, KeyCode::Enter);
        views.wrote(outcome);
        frame_with(&store, &views)
    }

    #[tokio::test]
    async fn a_refusal_at_any_status_cannot_hand_the_pair_to_the_screen() {
        // The owner never quotes what was typed, and C1 pins that at every one
        // of its refusals. Nothing on this path is *guaranteed* to be the
        // owner: a proxy in front of the desk answers with a page of its own,
        // and 401, 413, 502 and 504 are far likelier from one than 400.
        //
        // The first version of this guard scrubbed inside the 400 arm, and the
        // first version of this test asserted the pair was absent from
        // sentences that never contained it — a tautology. So the fixture
        // carries both values upstream, and the statuses swept are the ones
        // that fall through to `Wrote::Failed`.
        let (key, secret) = pair();
        for typed in [key.expose(), secret.expose()] {
            assert!(
                ECHOED.contains(typed),
                "the fixture must carry {typed} upstream or absence proves nothing"
            );
        }
        for status in [400, 401, 502] {
            let owner = spawn_owner(status, ECHOED);
            let client = WriteClient::new(&owner.base).unwrap();
            let outcome = perform(&client, login_cmd(false))
                .await
                .expect("a login always answers");
            // Three surfaces, because the pair reaches an operator through
            // three different renderers and the first fix covered one of them:
            // the outcome a log line would carry, the toast, and the form's
            // own note.
            let printed = format!("{outcome:?}");
            let toast = atlas::ui::widgets::toast::for_event(&AppEvent::Wrote(outcome.clone()))
                .expect("a refused login owes the operator a box");
            let note = form_note_after(&outcome);
            // The positive control. A frame with no box drawn in it would pass
            // every absence assertion below for the wrong reason — the way a
            // pin of this shape dies quietly.
            assert!(note.contains("ALPACA LOGIN"), "{status}: no form drawn");
            for surface in [&printed, &format!("{toast:?}"), &note] {
                for typed in [key.expose(), secret.expose()] {
                    assert!(!surface.contains(typed), "{status}: {surface}");
                }
            }
            // And it says why there is nothing to read, rather than going
            // blank: a refusal an operator cannot act on is bad enough without
            // also being unexplained.
            assert!(
                printed.contains("quoted what was typed"),
                "{status}: {printed}"
            );
            // One word, because the note is read off a rendered frame and the
            // sentence wraps across rows inside the box.
            assert!(note.contains("quoted"), "{status}: {note}");
        }
    }

    #[tokio::test]
    async fn testing_a_login_is_a_verdict_and_a_venue_that_says_no_is_one_too() {
        // `/api/alpaca/test` always answers 200 — a rejected key, a silent
        // venue and a missing profile are all results with a sentence, which is
        // why this reads `ok` rather than the status code.
        let owner = spawn_owner(
            200,
            r#"{"ok": true, "account_masked": "…7788", "status": "ACTIVE",
                "buying_power": 200000.0, "currency": "USD"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let verdict = client.test_alpaca().await.unwrap();
        assert!(verdict.ok);
        assert!(verdict.summary.contains("7788"), "{verdict:?}");
        assert!(verdict.summary.contains("$200,000.00"), "{verdict:?}");
        assert_eq!(owner.only().path, "/api/alpaca/test");

        let owner = spawn_owner(
            200,
            r#"{"ok": false, "reason": "rejected by alpaca — that key and secret are not a valid paper login"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let verdict = client.test_alpaca().await.unwrap();
        assert!(!verdict.ok);
        assert!(
            verdict.summary.contains("rejected by alpaca"),
            "{verdict:?}"
        );

        // `Some("")` is absent, exactly as everywhere else: a verdict with a
        // blank line under it says less than the fallback sentence does.
        let owner = spawn_owner(200, r#"{"ok": false, "reason": ""}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        assert!(!client.test_alpaca().await.unwrap().summary.is_empty());

        // And a 200 that will not say whether it worked is a broken contract,
        // for the same reason an execution without `executed` is: both guesses
        // are indefensible.
        let owner = spawn_owner(200, r#"{"account_masked": "…7788"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        assert!(client.test_alpaca().await.is_err());
    }

    /// One login command, built the way the form builds it.
    fn login_cmd(replace: bool) -> Command {
        let (key, secret) = pair();
        Command::AlpacaLogin {
            key,
            secret,
            replace,
        }
    }

    #[tokio::test]
    async fn each_credential_command_reports_what_the_owner_said_and_nothing_it_invented() {
        let owner = spawn_owner(200, STORED);
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, login_cmd(false)).await,
            Some(Wrote::LoggedIn {
                usable: true,
                note: "paper key ending 4f21".into(),
            })
        );
        // Driven through the seam rather than through the client, because this
        // is where "not asked" becomes a wire body: a `false` sent on every
        // login is one edit away from a `true` sent on every login, and the
        // route-level pin above cannot see the conversion at all.
        let seen: serde_json::Value = serde_json::from_str(&owner.only().body).unwrap();
        assert_eq!(
            seen.get("replace"),
            None,
            "a login nobody was asked about carried the consent flag"
        );

        // A login the owner stored and then reports it cannot read is the
        // "succeeded and did nothing" shape this client refuses to draw as a
        // receipt.
        let owner = spawn_owner(
            200,
            r#"{"data": "live", "book": "alpaca", "label": "LIVE · ALPACA BOOK",
                "credentials_ok": false, "credentials": "ALPACA_API_KEY_ID is set in the environment"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, login_cmd(false)).await {
            Some(Wrote::LoggedIn { usable, note }) => {
                assert!(!usable);
                assert!(note.contains("environment"), "{note}");
            }
            other => panic!("{other:?}"),
        }

        let owner = spawn_owner(400, CONSENT);
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, login_cmd(false)).await {
            Some(Wrote::LoginNeedsConsent { said }) => {
                assert!(said.contains("refresh token"), "{said}")
            }
            other => panic!("{other:?}"),
        }

        let owner = spawn_owner(
            400,
            r#"{"error": "that does not look like an alpaca key id"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, login_cmd(false)).await {
            Some(Wrote::LoginRefused { said }) => assert!(said.contains("key id"), "{said}"),
            other => panic!("{other:?}"),
        }

        // The consented re-POST is the same command with the flag set.
        let owner = spawn_owner(200, STORED);
        let client = WriteClient::new(&owner.base).unwrap();
        perform(&client, login_cmd(true)).await;
        let body: serde_json::Value = serde_json::from_str(&owner.only().body).unwrap();
        assert_eq!(body["replace"], serde_json::json!(true));

        let owner = spawn_owner(
            200,
            r#"{"ok": true, "account_masked": "…7788", "status": "ACTIVE",
                "buying_power": 200000.0, "currency": "USD"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::TestAlpaca).await {
            Some(Wrote::Tested { ok, summary }) => {
                assert!(ok);
                assert!(summary.contains("7788"), "{summary}");
            }
            other => panic!("{other:?}"),
        }
        assert_eq!(owner.only().path, "/api/alpaca/test");
    }

    #[test]
    fn every_credential_outcome_reaches_the_operator_with_the_level_it_deserves() {
        // Invariant 10 at the toast seam: a `Wrote` variant with no arm is a
        // write nobody is told about. The levels are the content — a stored
        // login the owner cannot read is `Warn`, because a file that changed
        // and a desk that cannot trade is the "succeeded and did nothing" shape
        // this client refuses to draw as a receipt.
        //
        // The secrecy claim is *not* here. These sentences are hand-written and
        // never contained a credential, so asserting one is absent from them
        // proves nothing about the path that could carry one — that is
        // `a_refusal_at_any_status_cannot_hand_the_pair_to_the_screen`, which
        // drives an echoing owner.
        use atlas::ui::widgets::toast::Level;
        let cases = [
            (
                Wrote::LoggedIn {
                    usable: true,
                    note: "paper key ending 4f21".into(),
                },
                Level::Info,
            ),
            (
                Wrote::LoggedIn {
                    usable: false,
                    note: "ALPACA_API_KEY_ID is set in the environment".into(),
                },
                Level::Warn,
            ),
            (
                Wrote::LoginNeedsConsent {
                    said: "the active alpaca profile holds a browser login".into(),
                },
                Level::Warn,
            ),
            (
                Wrote::LoginRefused {
                    said: "that does not look like an alpaca key id".into(),
                },
                Level::Warn,
            ),
            (
                Wrote::Tested {
                    ok: true,
                    summary: "…7788 · ACTIVE · $200,000.00 USD".into(),
                },
                Level::Info,
            ),
            (
                Wrote::Tested {
                    ok: false,
                    summary: "rejected by alpaca".into(),
                },
                Level::Warn,
            ),
        ];
        for (outcome, level) in cases {
            let toast = atlas::ui::widgets::toast::for_event(&AppEvent::Wrote(outcome.clone()))
                .expect("every write outcome owes the operator a box");
            assert_eq!(toast.level, level, "{outcome:?}");
            assert!(!toast.message.is_empty(), "{outcome:?}");
            assert!(
                refetches(&AppEvent::Wrote(outcome.clone())),
                "{outcome:?} must bring the next poll forward"
            );
        }
    }

    #[test]
    fn a_typed_credential_cannot_be_printed_by_anything_that_prints() {
        // `Command` derives `Debug`, the dispatcher traces, and a panic message
        // formats whatever it was given. The value therefore travels wrapped in
        // a type whose `Debug` is the redaction — not in a `String` that every
        // one of those would render in full.
        let (key, secret) = pair();
        assert_eq!(format!("{key:?}"), "Secret(<redacted>)");
        let printed = format!("{:?}", login_cmd(true));
        for typed in ["PKTEST0123456789", "s3cret/abcdefghijklmnopqrstuv"] {
            assert!(!printed.contains(typed), "{printed}");
        }
        // And the value is still there to be sent — a redaction that lost it
        // would pass this test and store nothing.
        assert_eq!(secret.expose(), "s3cret/abcdefghijklmnopqrstuv");
    }

    // -- the rights the operator lends atlas --------------------------------

    #[tokio::test]
    async fn one_right_travels_as_one_boolean_key_on_the_owners_own_route() {
        // One key per call, because the owner records one `desk.rights_changed`
        // row per changed field: a body carrying two would put two decisions
        // behind one keystroke. And a *boolean*, never "yes" or 1 — the route
        // refuses those rather than reading them as a grant, which is the
        // posture's precedent.
        let owner = spawn_owner(
            200,
            r#"{"rights": {"web": true, "workflows": false, "build": true},
                "path": "/state/atlas_rights.json"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let applied = client.set_right("workflows", false).await.unwrap();
        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/atlas/rights");
        assert_eq!(seen.body, r#"{"workflows":false}"#);
        // The owner's own object, never the request's echo: it writes all three
        // keys, and a receipt composed here would report a grant a partial
        // write never made.
        match applied {
            Rights::Applied(flags) => {
                assert_eq!(flags.web, Some(true));
                assert_eq!(flags.workflows, Some(false));
                assert_eq!(flags.build, Some(true));
            }
            other => panic!("{other:?}"),
        }
    }

    #[tokio::test]
    async fn a_right_the_owner_will_not_record_is_a_refusal_and_not_a_failure() {
        // Both of the owner's considered noes, and neither is a broken request:
        // the 400 names the rights this desk has, and the 403 says who sets
        // them. Folded into `Err` they would arrive as "the owner refused with
        // 400: {…}" — the remedy buried in a transport error nobody can act on.
        for (status, said) in [
            (
                400,
                "banana is not a right this desk has — the rights are web, workflows, build",
            ),
            (
                403,
                "Atlas does not set its own rights — the operator sets them on the desk, \
                 in Settings ▸ MODELS",
            ),
        ] {
            let owner = spawn_owner(status, serde_json::json!({"error": said}).to_string());
            let client = WriteClient::new(&owner.base).unwrap();
            match client.set_right("web", true).await {
                Ok(Rights::Rejected(back)) => assert_eq!(back, said),
                other => panic!("{status}: {other:?}"),
            }
        }
    }

    #[tokio::test]
    async fn a_broken_owner_is_an_error_and_a_200_with_no_rights_still_applied() {
        // A 500 is the owner breaking mid-write, which is not a decision about
        // the request: it reaches the card as a failed write and the toggle is
        // never reported as landed.
        let owner = spawn_owner(
            500,
            r#"{"error": "the rights file is not readable as JSON"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert!(client.set_right("build", false).await.is_err());

        // But a 200 that did not say where the rights stand is not a contract
        // failure worth refusing the change over — the change *happened*. The
        // flags come back absent and the refetch behind the write is what says
        // what is now on disk.
        let quiet = spawn_owner(200, "{}");
        let client = WriteClient::new(&quiet.base).unwrap();
        match client.set_right("build", false).await {
            Ok(Rights::Applied(flags)) => assert_eq!(flags.build, None),
            other => panic!("{other:?}"),
        }
    }

    // -- the standing grant a fill may happen under -------------------------

    #[tokio::test]
    async fn revoking_travels_as_a_reason_on_the_owners_own_route() {
        // A reason and nothing else. No id, because the owner holds one live
        // grant and is the only thing that knows which — a body naming one
        // could revoke the grant a card read seconds ago rather than the one
        // that is live now — and no ceilings, because this client composes no
        // grant at all.
        let owner = spawn_owner(
            200,
            r#"{"grant": {"grant_id": "9f31c0aa4b7d2e61", "revoked_at":
                "2026-09-01T12:00:00+00:00"}}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let revoked = client
            .revoke_authority("revoked by the operator on the desk")
            .await
            .unwrap();
        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/desk/authority/revoke");
        assert_eq!(
            seen.body,
            r#"{"reason":"revoked by the operator on the desk"}"#
        );
        // The owner's own id, off its own answer.
        match revoked {
            Authority::Revoked(id) => assert_eq!(id.as_deref(), Some("9f31c0aa4b7d2e61")),
            other => panic!("{other:?}"),
        }
    }

    #[tokio::test]
    async fn a_revocation_the_owner_will_not_make_is_a_refusal_and_not_a_failure() {
        // Both of the owner's considered noes. Folded into `Err` they would
        // arrive as "the owner refused with 400: {…}" — the remedy buried in a
        // transport error nobody can act on — and the 400 here is the desk
        // already being in the state the key was asking for.
        for (status, said) in [
            (400, "there is no standing grant to revoke"),
            (
                403,
                "a chat may not revoke a standing grant — the operator does, on the desk",
            ),
        ] {
            let owner = spawn_owner(status, serde_json::json!({"error": said}).to_string());
            let client = WriteClient::new(&owner.base).unwrap();
            match client.revoke_authority("why").await {
                Ok(Authority::Rejected(back)) => assert_eq!(back, said),
                other => panic!("{status}: {other:?}"),
            }
        }
        // A 500 is the owner breaking mid-write, which is not a decision about
        // the request: it reaches the card as a failed write and the grant is
        // never reported as withdrawn.
        let broken = spawn_owner(500, r#"{"error": "the registry is locked"}"#);
        let client = WriteClient::new(&broken.base).unwrap();
        assert!(client.revoke_authority("why").await.is_err());
        // And a 200 that named no grant still revoked: the change *happened*,
        // and the poll behind the write is what says the card now holds none.
        let quiet = spawn_owner(200, "{}");
        let client = WriteClient::new(&quiet.base).unwrap();
        match client.revoke_authority("why").await {
            Ok(Authority::Revoked(id)) => assert_eq!(id, None),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn nothing_but_a_write_outcome_refetches_from_here() {
        // The stream already nudges the poller for the durable kinds
        // (`net::sse::REFETCH_KINDS`), and a second rule for the same events in
        // a second place is how the two come to disagree.
        assert!(!refetches(&AppEvent::Tick));
        assert!(!refetches(&AppEvent::Resize));
        assert!(!refetches(&AppEvent::ConnUp(atlas::bus::Channel::Owner)));
        assert!(!refetches(&AppEvent::Sse(atlas::bus::SseEvent {
            kind: "plan_executed".into(),
            payload: serde_json::json!({}),
            ts: None,
            id: None,
        })));
        assert!(!refetches(&AppEvent::Snapshot(Box::new(snapshot()))));
    }
}
