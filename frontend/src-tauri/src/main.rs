#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

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
fn show_pet_menu(
    window: WebviewWindow,
    menu: State<'_, PetMenu>,
    position: LogicalPosition<f64>,
) -> tauri::Result<()> {
    window.popup_menu_at(&menu.0, position)
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
        .invoke_handler(tauri::generate_handler![show_pet_menu])
        .build(tauri::generate_context!())
        .expect("error while building AI Gaming Pet");

    application.run(|app, event| {
        if let RunEvent::Exit = event {
            if let Err(error) = app.global_shortcut().unregister_all() {
                eprintln!("failed to unregister global shortcuts during shutdown: {error}");
            }
        }
    });
}
