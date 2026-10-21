"""Tk email/password dialog for delta_force_native_login.

Posts ("native_login_done", result) or ("native_login_error", message) to the
app's msg_queue, same pattern as the browser login. The password lives only in
the Entry widget and the worker's local variable; it's cleared on submit.
"""
import queue
import threading
import tkinter as tk
from tkinter import ttk

import delta_force_native_login as nl


def open_login_dialog(root, colors, msg_queue, on_cancel=None):
    c = colors or {}
    bg = c.get("SURFACE", "#1e1e24")
    fg = c.get("FG", "#e8e8ee")

    win = tk.Toplevel(root)
    win.title("Log in")
    win.configure(bg=bg)
    win.resizable(False, False)
    win.transient(root)

    tk.Label(win, text="Log in with your Delta Force account", bg=bg, fg=fg,
             font=("Segoe UI", 11, "bold")).pack(padx=24, pady=(18, 4))
    tk.Label(win, text="Sent only to the official login server. Your password "
                       "is never saved.", bg=bg, fg=fg, wraplength=300,
             font=("Segoe UI", 9)).pack(padx=24, pady=(0, 12))

    email_var, pw_var = tk.StringVar(), tk.StringVar()
    ttk.Entry(win, textvariable=email_var, width=36).pack(padx=24, pady=4)
    pw = ttk.Entry(win, textvariable=pw_var, show="\u2022", width=36)
    pw.pack(padx=24, pady=4)

    status = tk.Label(win, text="", bg=bg, fg=fg, wraplength=300,
                      font=("Segoe UI", 9))
    status.pack(padx=24, pady=(6, 0))

    btn = ttk.Button(win, text="Log in")
    btn.pack(padx=24, pady=(10, 18))

    def finish(kind, payload):
        msg_queue.put((kind, payload))
        try:
            win.destroy()
        except Exception:
            pass

    def submit(_evt=None):
        email, password = email_var.get().strip(), pw_var.get()
        if not email or not password:
            status.configure(text="Enter your email and password.")
            return
        pw_var.set("")                      # clear the widget immediately
        btn.state(["disabled"])
        status.configure(text="Logging in… a verification window may appear.")

        results = queue.Queue()      # worker thread -> Tk thread (thread-safe)

        def poll():
            # Runs on the Tk thread. Never touch widgets from the worker.
            if not win.winfo_exists():
                return
            try:
                kind, payload = results.get_nowait()
            except queue.Empty:
                win.after(100, poll)
                return
            if kind == "ok":
                finish("native_login_done", payload)
            else:
                btn.state(["!disabled"])
                status.configure(text=payload)

        def worker(email=email, password=password):
            try:
                res = nl.login(email, password)
                nl.save_session(res)
                results.put(("ok", res))
            except nl.LoginError as err:
                if err.ret in (nl.RET_NEED_CAPTCHA, nl.RET_MACHINE_CHECK):
                    msg = "Verification didn't complete. Please try again."
                elif "cancel" in str(err).lower():
                    msg = "Verification was cancelled."
                elif err.ret is not None:
                    msg = f"Login failed. Check your email and password. (code {err.ret})"
                else:
                    msg = "Login failed. Please try again."
                results.put(("fail", msg))
            except Exception:
                results.put(("fail", "Couldn't complete the login. Check "
                                     "your connection and try again."))

        win.after(100, poll)
        threading.Thread(target=worker, daemon=True).start()

    btn.configure(command=submit)
    win.bind("<Return>", submit)
    win.protocol("WM_DELETE_WINDOW",
                 lambda: (win.destroy(), on_cancel and on_cancel()))
    win.grab_set()
    pw.focus_set()
    return win
