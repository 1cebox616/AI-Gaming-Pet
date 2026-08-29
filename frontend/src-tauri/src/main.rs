#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::Duration,
};

use serde::Deserialize;
use tauri::{
    menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    App, AppHandle, Emitter, LogicalPosition, Manager, PhysicalPosition, RunEvent, State,
    WebviewWindow, Wry,
};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

const WINDOW_LABEL: &str = "main";
const PET_NEXT_EXPRESSION_EVENT: &str = "pet-next-expression";
const SPEAK_NEXT_IDLE_LINE_EVENT: &str = "speak-next-idle-line";
const SET_SPEECH_ENABLED_EVENT: &str = "set-speech-enabled";
const SET_MUTED_EVENT: &str = "set-muted";
const PET_WINDOW_VISIBILITY_EVENT: &str = "pet-window-visibility";
const GAME_STATUS_ITEM_ID: &str = "game-status";
const LLM_MODE_ITEM_ID: &str = "llm-mode";
const LLM_COST_ITEM_ID: &str = "llm-cost";
const DISCONNECTED_GAME_STATUS: &str = "CS2：未知（后端未连接）";
const DISCONNECTED_LLM_STATUS: &str = "—";
const WINDOW_MARGIN_DIP: u32 = 40;
const CURSOR_POLL_INTERVAL: Duration = Duration::from_millis(20);

#[cfg(target_os = "windows")]
const VK_LBUTTON: i32 = 0x01;

#[cfg(target_os = "windows")]
#[link(name = "user32")]
unsafe extern "system" {
    fn GetAsyncKeyState(virtual_key: i32) -> i16;
}

#[derive(Clone, Copy)]
struct LogicalInteractionRect {
    x: f64,
    y: f64,
    width: f64,
    height: f64,
}

impl LogicalInteractionRect {
    fn contains(self, x: f64, y: f64) -> bool {
        x >= self.x && x <= self.x + self.width && y >= self.y && y <= self.y + self.height
    }
}

fn physical_cursor_to_window_logical(
    cursor_position: PhysicalPosition<f64>,
    window_position: PhysicalPosition<i32>,
    scale_factor: f64,
) -> LogicalPosition<f64> {
    LogicalPosition::new(
        (cursor_position.x - f64::from(window_position.x)) / scale_factor,
        (cursor_position.y - f64::from(window_position.y)) / scale_factor,
    )
}

struct PetInteractionStateInner {
    interaction_rect: Mutex<Option<LogicalInteractionRect>>,
    is_dragging: AtomicBool,
    stop_requested: AtomicBool,
}

#[derive(Clone)]
struct PetInteractionState {
    inner: Arc<PetInteractionStateInner>,
}

impl PetInteractionState {
    fn new() -> Self {
        Self {
            inner: Arc::new(PetInteractionStateInner {
                interaction_rect: Mutex::new(None),
                is_dragging: AtomicBool::new(false),
                stop_requested: AtomicBool::new(false),
            }),
        }
    }
}

#[derive(Clone, Copy)]
enum PetMenuAction {
    Speak,
    NextExpression,
    Speech,
    AutoSpeak,
    ToggleWindow,
    Quit,
}

#[derive(Clone, Copy)]
enum PetMenuEntry {
    GameStatus,
    CurrentGame,
    LlmMode,
    LlmCost,
    Action(PetMenuAction),
    Separator,
}

const PET_MENU_LAYOUT: [PetMenuEntry; 12] = [
    PetMenuEntry::GameStatus,
    PetMenuEntry::CurrentGame,
    PetMenuEntry::LlmMode,
    PetMenuEntry::LlmCost,
    PetMenuEntry::Action(PetMenuAction::Speak),
    PetMenuEntry::Action(PetMenuAction::NextExpression),
    PetMenuEntry::Separator,
    PetMenuEntry::Action(PetMenuAction::Speech),
    PetMenuEntry::Action(PetMenuAction::AutoSpeak),
    PetMenuEntry::Separator,
    PetMenuEntry::Action(PetMenuAction::ToggleWindow),
    PetMenuEntry::Action(PetMenuAction::Quit),
];

impl PetMenuAction {
    fn id(self) -> &'static str {
        match self {
            Self::Speak => "speak",
            Self::NextExpression => "next-expression",
            Self::Speech => "speech",
            Self::AutoSpeak => "auto-speak",
            Self::ToggleWindow => "toggle-window",
            Self::Quit => "quit",
        }
    }

    fn text(self) -> &'static str {
        match self {
            Self::Speak => "说句话",
            Self::NextExpression => "换个表情",
            Self::Speech => "语音",
            Self::AutoSpeak => "自动说话",
            Self::ToggleWindow => "显示/隐藏",
            Self::Quit => "退出",
        }
    }

    fn is_checkable(self) -> bool {
        matches!(self, Self::Speech | Self::AutoSpeak)
    }

    fn from_id(id: &str) -> Option<Self> {
        PET_MENU_LAYOUT.into_iter().find_map(|entry| match entry {
            PetMenuEntry::Action(action) if action.id() == id => Some(action),
            _ => None,
        })
    }

    fn handle(self, app: &AppHandle, menu: &PetMenu) {
        match self {
            Self::Speak => request_next_idle_line(app),
            Self::NextExpression => request_next_pet_expression(app),
            Self::Speech => request_speech_switch(app, menu),
            Self::AutoSpeak => request_automatic_speech_switch(app, menu),
            Self::ToggleWindow => toggle_main_window_and_log(app),
            Self::Quit => unregister_shortcuts_and_exit(app),
        }
    }
}

#[derive(Clone, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct BackendMenuState {
    connected: bool,
    speech_enabled: bool,
    muted: bool,
    game_id: String,
    game_status: String,
    llm_mode: String,
    llm_cost: String,
}

struct PetMenu {
    menu: Menu<Wry>,
    current_game_cs2_item: CheckMenuItem<Wry>,
    current_game_generic_item: CheckMenuItem<Wry>,
    game_status_item: MenuItem<Wry>,
    llm_mode_item: MenuItem<Wry>,
    llm_cost_item: MenuItem<Wry>,
    speech_item: CheckMenuItem<Wry>,
    auto_speak_item: CheckMenuItem<Wry>,
    backend_state: Mutex<BackendMenuState>,
}

impl PetMenu {
    fn update_backend_state(&self, state: &BackendMenuState) -> Result<(), String> {
        let mut current_state = self
            .backend_state
            .lock()
            .map_err(|_| "pet menu state lock is unavailable")?;
        *current_state = state.clone();
        drop(current_state);

        self.game_status_item
            .set_text(&state.game_status)
            .map_err(|error| error.to_string())?;
        self.llm_mode_item
            .set_text(&state.llm_mode)
            .map_err(|error| error.to_string())?;
        self.llm_mode_item
            .set_enabled(false)
            .map_err(|error| error.to_string())?;
        self.current_game_cs2_item
            .set_checked(state.connected && state.game_id == "cs2")
            .map_err(|error| error.to_string())?;
        self.current_game_generic_item
            .set_checked(state.connected && state.game_id == "generic")
            .map_err(|error| error.to_string())?;
        self.llm_cost_item
            .set_text(&state.llm_cost)
            .map_err(|error| error.to_string())?;
        self.llm_cost_item
            .set_enabled(false)
            .map_err(|error| error.to_string())?;
        self.game_status_item
            .set_enabled(false)
            .map_err(|error| error.to_string())?;
        self.speech_item
            .set_enabled(state.connected)
            .map_err(|error| error.to_string())?;
        self.auto_speak_item
            .set_enabled(state.connected)
            .map_err(|error| error.to_string())?;
        self.speech_item
            .set_checked(state.connected && state.speech_enabled)
            .map_err(|error| error.to_string())?;
        self.auto_speak_item
            .set_checked(state.connected && !state.muted)
            .map_err(|error| error.to_string())?;
        Ok(())
    }

    fn backend_state(&self) -> Result<BackendMenuState, String> {
        self.backend_state
            .lock()
            .map(|state| state.clone())
            .map_err(|_| "pet menu state lock is unavailable".into())
    }
}

fn toggle_main_window(app: &AppHandle) -> tauri::Result<()> {
    let Some(window) = app.get_webview_window(WINDOW_LABEL) else {
        eprintln!("main window is unavailable");
        return Ok(());
    };

    if window.is_visible()? {
        window.hide()?;
        app.emit_to(WINDOW_LABEL, PET_WINDOW_VISIBILITY_EVENT, false)?;
    } else {
        window.show()?;
        app.emit_to(WINDOW_LABEL, PET_WINDOW_VISIBILITY_EVENT, true)?;
    }

    Ok(())
}

fn toggle_main_window_and_log(app: &AppHandle) {
    if let Err(error) = toggle_main_window(app) {
        eprintln!("failed to toggle the main window: {error}");
    }
}

fn request_next_pet_expression(app: &AppHandle) {
    if let Err(error) = app.emit_to(WINDOW_LABEL, PET_NEXT_EXPRESSION_EVENT, ()) {
        eprintln!("failed to request the next pet expression: {error}");
    }
}

fn request_next_idle_line(app: &AppHandle) {
    if let Err(error) = app.emit_to(WINDOW_LABEL, SPEAK_NEXT_IDLE_LINE_EVENT, ()) {
        eprintln!("failed to request the next idle line: {error}");
    }
}

fn request_speech_switch(app: &AppHandle, menu: &PetMenu) {
    let state = match menu.backend_state() {
        Ok(state) if state.connected => state,
        Ok(_) => return,
        Err(error) => {
            eprintln!("failed to read pet menu state: {error}");
            return;
        }
    };

    if let Err(error) = menu.speech_item.set_checked(state.speech_enabled) {
        eprintln!("failed to restore authoritative speech menu state: {error}");
    }
    if let Err(error) = app.emit_to(
        WINDOW_LABEL,
        SET_SPEECH_ENABLED_EVENT,
        !state.speech_enabled,
    ) {
        eprintln!("failed to request a speech switch change: {error}");
    }
}

fn request_automatic_speech_switch(app: &AppHandle, menu: &PetMenu) {
    let state = match menu.backend_state() {
        Ok(state) if state.connected => state,
        Ok(_) => return,
        Err(error) => {
            eprintln!("failed to read pet menu state: {error}");
            return;
        }
    };

    if let Err(error) = menu.auto_speak_item.set_checked(!state.muted) {
        eprintln!("failed to restore authoritative automatic speech menu state: {error}");
    }
    if let Err(error) = app.emit_to(WINDOW_LABEL, SET_MUTED_EVENT, !state.muted) {
        eprintln!("failed to request an automatic speech switch change: {error}");
    }
}

fn unregister_shortcuts_and_exit(app: &AppHandle) {
    if let Err(error) = app.global_shortcut().unregister_all() {
        eprintln!("failed to unregister global shortcuts: {error}");
    }

    stop_cursor_interaction_monitor(app);
    app.exit(0);
}

fn build_pet_menu(app: &App) -> tauri::Result<PetMenu> {
    let menu = Menu::new(app)?;
    let mut game_status_item = None;
    let mut current_game_cs2_item = None;
    let mut current_game_generic_item = None;
    let mut llm_mode_item = None;
    let mut llm_cost_item = None;
    let mut speech_item = None;
    let mut auto_speak_item = None;

    for entry in PET_MENU_LAYOUT {
        match entry {
            PetMenuEntry::GameStatus => {
                let item = MenuItem::with_id(
                    app,
                    GAME_STATUS_ITEM_ID,
                    DISCONNECTED_GAME_STATUS,
                    false,
                    None::<&str>,
                )?;
                menu.append(&item)?;
                game_status_item = Some(item);
            }
            PetMenuEntry::CurrentGame => {
                let current_game = Submenu::new(app, "当前游戏", true)?;
                let selected = CheckMenuItem::with_id(
                    app,
                    "current-game-cs2",
                    "CS2",
                    true,
                    true,
                    None::<&str>,
                )?;
                let generic = CheckMenuItem::with_id(
                    app,
                    "current-game-generic",
                    "通用视觉",
                    true,
                    false,
                    None::<&str>,
                )?;
                let unavailable = MenuItem::with_id(
                    app,
                    "current-game-warthunder",
                    "战争雷霆（未安装）",
                    false,
                    None::<&str>,
                )?;
                current_game.append(&selected)?;
                current_game.append(&generic)?;
                current_game.append(&unavailable)?;
                menu.append(&current_game)?;
                current_game_cs2_item = Some(selected);
                current_game_generic_item = Some(generic);
            }
            PetMenuEntry::LlmMode => {
                let item = MenuItem::with_id(
                    app,
                    LLM_MODE_ITEM_ID,
                    DISCONNECTED_LLM_STATUS,
                    false,
                    None::<&str>,
                )?;
                menu.append(&item)?;
                llm_mode_item = Some(item);
            }
            PetMenuEntry::LlmCost => {
                let item = MenuItem::with_id(
                    app,
                    LLM_COST_ITEM_ID,
                    DISCONNECTED_LLM_STATUS,
                    false,
                    None::<&str>,
                )?;
                menu.append(&item)?;
                llm_cost_item = Some(item);
            }
            PetMenuEntry::Separator => {
                let separator = PredefinedMenuItem::separator(app)?;
                menu.append(&separator)?;
            }
            PetMenuEntry::Action(action) if action.is_checkable() => {
                let item = CheckMenuItem::with_id(
                    app,
                    action.id(),
                    action.text(),
                    false,
                    false,
                    None::<&str>,
                )?;
                menu.append(&item)?;
                match action {
                    PetMenuAction::Speech => speech_item = Some(item),
                    PetMenuAction::AutoSpeak => auto_speak_item = Some(item),
                    _ => unreachable!("only switch actions are checkable"),
                }
            }
            PetMenuEntry::Action(action) => {
                let item = MenuItem::with_id(app, action.id(), action.text(), true, None::<&str>)?;
                menu.append(&item)?;
            }
        }
    }

    Ok(PetMenu {
        menu,
        current_game_cs2_item: current_game_cs2_item
            .expect("CS2 must be present in the current-game menu"),
        current_game_generic_item: current_game_generic_item
            .expect("generic vision must be present in the current-game menu"),
        game_status_item: game_status_item
            .expect("game status must be present in the pet menu layout"),
        llm_mode_item: llm_mode_item.expect("LLM mode must be present in the pet menu layout"),
        llm_cost_item: llm_cost_item.expect("LLM cost must be present in the pet menu layout"),
        speech_item: speech_item.expect("speech switch must be present in the pet menu layout"),
        auto_speak_item: auto_speak_item
            .expect("automatic speech switch must be present in the pet menu layout"),
        backend_state: Mutex::new(BackendMenuState::default()),
    })
}

#[tauri::command]
fn update_pet_menu_state(state: BackendMenuState, menu: State<'_, PetMenu>) -> Result<(), String> {
    menu.update_backend_state(&state)
}

#[tauri::command]
fn report_pet_interaction_region(
    x: f64,
    y: f64,
    width: f64,
    height: f64,
    interaction_state: State<'_, PetInteractionState>,
) -> Result<(), String> {
    if !x.is_finite()
        || !y.is_finite()
        || !width.is_finite()
        || !height.is_finite()
        || width <= 0.0
        || height <= 0.0
    {
        return Err("pet interaction region must be a finite, non-empty rectangle".into());
    }

    let mut interaction_rect = interaction_state
        .inner
        .interaction_rect
        .lock()
        .map_err(|_| "pet interaction region lock is unavailable")?;
    *interaction_rect = Some(LogicalInteractionRect {
        x,
        y,
        width,
        height,
    });
    Ok(())
}

#[tauri::command]
fn mark_pet_dragging(interaction_state: State<'_, PetInteractionState>) {
    interaction_state
        .inner
        .is_dragging
        .store(true, Ordering::Release);
}

#[tauri::command]
fn show_pet_menu(
    window: WebviewWindow,
    menu: State<'_, PetMenu>,
    position: LogicalPosition<f64>,
) -> tauri::Result<()> {
    window.popup_menu_at(&menu.menu, position)
}

#[cfg(target_os = "windows")]
fn is_left_mouse_button_down() -> bool {
    // SAFETY: GetAsyncKeyState is a read-only Windows API for the current process desktop.
    unsafe { GetAsyncKeyState(VK_LBUTTON) < 0 }
}

#[cfg(not(target_os = "windows"))]
fn is_left_mouse_button_down() -> bool {
    false
}

fn update_cursor_event_ignoring(
    window: &WebviewWindow,
    should_ignore: bool,
    current_state: &mut Option<bool>,
) {
    if *current_state == Some(should_ignore) {
        return;
    }

    match window.set_ignore_cursor_events(should_ignore) {
        Ok(()) => *current_state = Some(should_ignore),
        Err(error) => eprintln!("failed to update pet cursor event handling: {error}"),
    }
}

fn start_cursor_interaction_monitor(app: AppHandle, state: Arc<PetInteractionStateInner>) {
    thread::spawn(move || {
        let mut cursor_events_ignored = None;

        while !state.stop_requested.load(Ordering::Acquire) {
            let Some(window) = app.get_webview_window(WINDOW_LABEL) else {
                break;
            };

            if !window.is_visible().unwrap_or(false) {
                thread::sleep(CURSOR_POLL_INTERVAL);
                continue;
            }

            if state.is_dragging.load(Ordering::Acquire) {
                if is_left_mouse_button_down() {
                    update_cursor_event_ignoring(&window, false, &mut cursor_events_ignored);
                    thread::sleep(CURSOR_POLL_INTERVAL);
                    continue;
                }

                state.is_dragging.store(false, Ordering::Release);
            }

            let interaction_rect = match state.interaction_rect.lock() {
                Ok(interaction_rect) => *interaction_rect,
                Err(_) => {
                    eprintln!(
                        "pet interaction region lock is unavailable; stopping cursor monitor"
                    );
                    break;
                }
            };

            let Some(interaction_rect) = interaction_rect else {
                thread::sleep(CURSOR_POLL_INTERVAL);
                continue;
            };

            let cursor_position = match window.cursor_position() {
                Ok(position) => position,
                Err(error) => {
                    eprintln!("failed to read cursor position: {error}");
                    thread::sleep(CURSOR_POLL_INTERVAL);
                    continue;
                }
            };
            let window_position = match window.outer_position() {
                Ok(position) => position,
                Err(error) => {
                    eprintln!("failed to read pet window position: {error}");
                    thread::sleep(CURSOR_POLL_INTERVAL);
                    continue;
                }
            };
            let scale_factor = match window.scale_factor() {
                Ok(scale_factor) if scale_factor > 0.0 => scale_factor,
                Ok(_) => {
                    eprintln!("pet window reported an invalid scale factor");
                    thread::sleep(CURSOR_POLL_INTERVAL);
                    continue;
                }
                Err(error) => {
                    eprintln!("failed to read pet window scale factor: {error}");
                    thread::sleep(CURSOR_POLL_INTERVAL);
                    continue;
                }
            };

            let logical_cursor =
                physical_cursor_to_window_logical(cursor_position, window_position, scale_factor);
            let should_ignore = !interaction_rect.contains(logical_cursor.x, logical_cursor.y);
            update_cursor_event_ignoring(&window, should_ignore, &mut cursor_events_ignored);

            thread::sleep(CURSOR_POLL_INTERVAL);
        }
    });
}

fn stop_cursor_interaction_monitor(app: &AppHandle) {
    if let Some(interaction_state) = app.try_state::<PetInteractionState>() {
        interaction_state
            .inner
            .stop_requested
            .store(true, Ordering::Release);
    }
}

fn position_on_primary_monitor(window: &WebviewWindow) -> tauri::Result<()> {
    let Some(monitor) = window.primary_monitor()? else {
        eprintln!("primary monitor is unavailable; retaining the default window position");
        return Ok(());
    };

    let work_area = monitor.work_area();
    let window_size = window.outer_size()?;
    let margin_px = (f64::from(WINDOW_MARGIN_DIP) * monitor.scale_factor()).round() as u32;
    let horizontal_offset = work_area
        .size
        .width
        .saturating_sub(window_size.width.saturating_add(margin_px));
    let vertical_offset = work_area
        .size
        .height
        .saturating_sub(window_size.height.saturating_add(margin_px));

    window.set_position(PhysicalPosition::new(
        work_area.position.x + horizontal_offset as i32,
        work_area.position.y + vertical_offset as i32,
    ))
}

fn configure_tray(app: &App, menu: &Menu<Wry>) -> tauri::Result<()> {
    let icon = app
        .default_window_icon()
        .expect("the configured Windows application icon must be present")
        .clone();

    TrayIconBuilder::with_id("main-tray")
        .icon(icon)
        .tooltip("AI Gaming Pet")
        .menu(menu)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_main_window_and_log(tray.app_handle());
            }
        })
        .build(app)?;

    Ok(())
}

fn main() {
    let application = tauri::Builder::default()
        .on_menu_event(|app, event| {
            if matches!(
                event.id().as_ref(),
                "current-game-cs2" | "current-game-generic"
            ) {
                let menu = app.state::<PetMenu>();
                match menu.backend_state() {
                    Ok(state) => {
                        if let Err(error) = menu
                            .current_game_cs2_item
                            .set_checked(state.connected && state.game_id == "cs2")
                        {
                            eprintln!("failed to restore the CS2 game item: {error}");
                        }
                        if let Err(error) = menu
                            .current_game_generic_item
                            .set_checked(state.connected && state.game_id == "generic")
                        {
                            eprintln!("failed to restore the generic game item: {error}");
                        }
                    }
                    Err(error) => {
                        eprintln!("failed to restore current-game menu state: {error}");
                    }
                }
                return;
            }
            if let Some(action) = PetMenuAction::from_id(event.id().as_ref()) {
                let menu = app.state::<PetMenu>();
                action.handle(app, &menu);
            }
        })
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        toggle_main_window_and_log(app);
                    }
                })
                .build(),
        )
        .setup(|app| {
            let interaction_state = PetInteractionState::new();
            app.manage(interaction_state.clone());
            start_cursor_interaction_monitor(app.handle().clone(), interaction_state.inner);

            let shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyP);
            app.global_shortcut().register(shortcut)?;
            let menu = build_pet_menu(app)?;
            let tray_menu = menu.menu.clone();
            app.manage(menu);
            configure_tray(app, &tray_menu)?;

            if let Some(window) = app.get_webview_window(WINDOW_LABEL) {
                window.show()?;
                position_on_primary_monitor(&window)?;
            } else {
                eprintln!("main window is unavailable during startup");
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            report_pet_interaction_region,
            mark_pet_dragging,
            show_pet_menu,
            update_pet_menu_state
        ])
        .build(tauri::generate_context!())
        .expect("error while building AI Gaming Pet");

    application.run(|app, event| {
        if let RunEvent::Exit = event {
            stop_cursor_interaction_monitor(app);
            if let Err(error) = app.global_shortcut().unregister_all() {
                eprintln!("failed to unregister global shortcuts during shutdown: {error}");
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_RECT: LogicalInteractionRect = LogicalInteractionRect {
        x: 10.0,
        y: 20.0,
        width: 30.0,
        height: 40.0,
    };

    fn physical_cursor_for_logical(
        logical_x: f64,
        logical_y: f64,
        window_position: PhysicalPosition<i32>,
        scale_factor: f64,
    ) -> PhysicalPosition<f64> {
        PhysicalPosition::new(
            f64::from(window_position.x) + logical_x * scale_factor,
            f64::from(window_position.y) + logical_y * scale_factor,
        )
    }

    #[test]
    fn interaction_rect_contains_interior_and_all_inclusive_boundaries() {
        assert!(TEST_RECT.contains(25.0, 40.0));
        assert!(TEST_RECT.contains(10.0, 20.0));
        assert!(TEST_RECT.contains(40.0, 20.0));
        assert!(TEST_RECT.contains(10.0, 60.0));
        assert!(TEST_RECT.contains(40.0, 60.0));

        assert!(!TEST_RECT.contains(9.99, 40.0));
        assert!(!TEST_RECT.contains(40.01, 40.0));
        assert!(!TEST_RECT.contains(25.0, 19.99));
        assert!(!TEST_RECT.contains(25.0, 60.01));
    }

    #[test]
    fn physical_cursor_conversion_covers_common_scale_factors() {
        let window_position = PhysicalPosition::new(-200, 300);

        for scale_factor in [1.0, 1.25, 1.5] {
            let physical_cursor =
                physical_cursor_for_logical(32.0, 48.0, window_position, scale_factor);
            let logical_cursor =
                physical_cursor_to_window_logical(physical_cursor, window_position, scale_factor);

            assert!((logical_cursor.x - 32.0).abs() < f64::EPSILON);
            assert!((logical_cursor.y - 48.0).abs() < f64::EPSILON);
        }
    }

    #[test]
    fn backend_menu_state_retains_the_extended_llm_display_values() {
        let state = BackendMenuState {
            connected: true,
            speech_enabled: true,
            muted: false,
            game_id: "generic".into(),
            game_status: "正在观看".into(),
            llm_mode: "当前：AI 模式".into(),
            llm_cost: "本次花费：$0.0123".into(),
        };

        assert_eq!(state.game_id, "generic");
        assert_eq!(state.llm_mode, "当前：AI 模式");
        assert_eq!(state.llm_cost, "本次花费：$0.0123");
    }

    // 锁住前后端菜单状态字段按名字对齐，避免同类型字段按位置静默错位。
    #[test]
    fn backend_menu_state_deserializes_camel_case_ipc_fields_by_name() {
        let state: BackendMenuState = serde_json::from_str(
            r#"{
                "connected": true,
                "speechEnabled": false,
                "muted": true,
                "gameId": "generic",
                "gameStatus": "正在观看",
                "llmMode": "当前：AI 模式",
                "llmCost": "本次花费：$0.0456"
            }"#,
        )
        .expect("camelCase backend menu state must deserialize");

        assert!(state.connected);
        assert!(!state.speech_enabled);
        assert!(state.muted);
        assert_eq!(state.game_id, "generic");
        assert_eq!(state.game_status, "正在观看");
        assert_eq!(state.llm_mode, "当前：AI 模式");
        assert_eq!(state.llm_cost, "本次花费：$0.0456");
    }

    #[test]
    fn converted_cursor_preserves_rect_edges_across_scale_factors() {
        let window_position = PhysicalPosition::new(120, -80);

        for scale_factor in [1.0, 1.25, 1.5] {
            for (logical_x, logical_y, expected_inside) in [
                (25.0, 40.0, true),
                (10.0, 20.0, true),
                (40.0, 60.0, true),
                (9.9, 40.0, false),
                (40.1, 40.0, false),
            ] {
                let physical_cursor = physical_cursor_for_logical(
                    logical_x,
                    logical_y,
                    window_position,
                    scale_factor,
                );
                let converted = physical_cursor_to_window_logical(
                    physical_cursor,
                    window_position,
                    scale_factor,
                );

                assert_eq!(
                    TEST_RECT.contains(converted.x, converted.y),
                    expected_inside,
                    "scale factor {scale_factor}, logical point ({logical_x}, {logical_y})",
                );
            }
        }
    }
}
