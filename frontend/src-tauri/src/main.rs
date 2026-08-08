#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    App, AppHandle, Manager, PhysicalPosition, RunEvent, WebviewWindow,
};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

const WINDOW_LABEL: &str = "main";
const TRAY_TOGGLE_ID: &str = "toggle-window";
const TRAY_QUIT_ID: &str = "quit";
const WINDOW_MARGIN_DIP: u32 = 40;

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

fn unregister_shortcuts_and_exit(app: &AppHandle) {
    if let Err(error) = app.global_shortcut().unregister_all() {
        eprintln!("failed to unregister global shortcuts: {error}");
    }

    app.exit(0);
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

fn configure_tray(app: &App) -> tauri::Result<()> {
    let toggle_item = MenuItem::with_id(app, TRAY_TOGGLE_ID, "显示/隐藏", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, TRAY_QUIT_ID, "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&toggle_item, &quit_item])?;
    let icon = app
        .default_window_icon()
        .expect("the configured Windows application icon must be present")
        .clone();

    TrayIconBuilder::with_id("main-tray")
        .icon(icon)
        .tooltip("AI Gaming Pet")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            TRAY_TOGGLE_ID => toggle_main_window_and_log(app),
            TRAY_QUIT_ID => unregister_shortcuts_and_exit(app),
            _ => {}
        })
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
            configure_tray(app)?;

            if let Some(window) = app.get_webview_window(WINDOW_LABEL) {
                window.show()?;
                position_on_primary_monitor(&window)?;
            } else {
                eprintln!("main window is unavailable during startup");
            }

            Ok(())
        })
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
