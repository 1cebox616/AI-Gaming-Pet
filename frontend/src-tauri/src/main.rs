#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::Duration,
};

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    App, AppHandle, Emitter, LogicalPosition, Manager, PhysicalPosition, RunEvent, State,
    WebviewWindow, Wry,
};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

const WINDOW_LABEL: &str = "main";
const PET_NEXT_EXPRESSION_EVENT: &str = "pet-next-expression";
const SPEAK_NEXT_IDLE_LINE_EVENT: &str = "speak-next-idle-line";
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
    ToggleWindow,
    Quit,
}

impl PetMenuAction {
    const ALL: [Self; 4] = [
        Self::Speak,
        Self::NextExpression,
        Self::ToggleWindow,
        Self::Quit,
    ];

    fn id(self) -> &'static str {
        match self {
            Self::Speak => "speak",
            Self::NextExpression => "next-expression",
            Self::ToggleWindow => "toggle-window",
            Self::Quit => "quit",
        }
    }

    fn text(self) -> &'static str {
        match self {
            Self::Speak => "说句话",
            Self::NextExpression => "换个表情",
            Self::ToggleWindow => "显示/隐藏",
            Self::Quit => "退出",
        }
    }

    fn from_id(id: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|action| action.id() == id)
    }

    fn handle(self, app: &AppHandle) {
        match self {
            Self::Speak => request_next_idle_line(app),
            Self::NextExpression => request_next_pet_expression(app),
            Self::ToggleWindow => toggle_main_window_and_log(app),
            Self::Quit => unregister_shortcuts_and_exit(app),
        }
    }
}

struct PetMenu(Menu<Wry>);

fn toggle_main_window(app: &AppHandle) -> tauri::Result<()> {
    let Some(window) = app.get_webview_window(WINDOW_LABEL) else {
        eprintln!("main window is unavailable");
        return Ok(());
    };

    if window.is_visible()? {
        window.hide()?;
    } else {
        window.show()?;
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

fn unregister_shortcuts_and_exit(app: &AppHandle) {
    if let Err(error) = app.global_shortcut().unregister_all() {
        eprintln!("failed to unregister global shortcuts: {error}");
    }

    stop_cursor_interaction_monitor(app);
    app.exit(0);
}

fn build_pet_menu(app: &App) -> tauri::Result<Menu<Wry>> {
    let menu = Menu::new(app)?;
    for action in PetMenuAction::ALL {
        let item = MenuItem::with_id(app, action.id(), action.text(), true, None::<&str>)?;
        menu.append(&item)?;
    }
    Ok(menu)
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
    window.popup_menu_at(&menu.0, position)
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

            let cursor_x = (cursor_position.x - f64::from(window_position.x)) / scale_factor;
            let cursor_y = (cursor_position.y - f64::from(window_position.y)) / scale_factor;
            let should_ignore = !interaction_rect.contains(cursor_x, cursor_y);
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
            if let Some(action) = PetMenuAction::from_id(event.id().as_ref()) {
                action.handle(app);
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
            app.manage(PetMenu(menu.clone()));
            configure_tray(app, &menu)?;

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
            show_pet_menu
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
