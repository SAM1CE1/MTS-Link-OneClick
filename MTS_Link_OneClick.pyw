# -*- coding: utf-8 -*-
"""MTS Link OneClick GUI. Pure tkinter, network access only to MTS Link assets."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import mts_backend as engine

DEFAULT_OUT = Path.home() / 'Downloads'
if not DEFAULT_OUT.is_dir():
    DEFAULT_OUT = Path.home()


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('MTS Link OneClick — запись в MP4')
        self.root.geometry('850x650')
        self.root.minsize(760, 570)
        self.root.configure(bg='#F4F7FB')
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.result = None
        self.url = tk.StringVar(value='')
        self.out = tk.StringVar(value=str(DEFAULT_OUT))
        self.status = tk.StringVar(value='Готово к загрузке')
        self._make_widgets()
        self.root.after(100, self.process_events)
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

    def _make_widgets(self):
        s = ttk.Style(self.root)
        try:
            s.theme_use('clam')
        except tk.TclError:
            pass
        s.configure('Primary.TButton', background='#1764DA', foreground='white',
                    font=('Segoe UI', 11, 'bold'), padding=(16, 12))
        s.map('Primary.TButton', background=[('active', '#0F52BC'), ('disabled', '#9BB7E1')])
        s.configure('Secondary.TButton', padding=(10, 9), font=('Segoe UI', 10))
        s.configure('TEntry', padding=8)
        s.configure('Horizontal.TProgressbar', troughcolor='#E5EBF3', background='#1764DA', borderwidth=0)

        container = tk.Frame(self.root, bg='#F4F7FB')
        container.pack(fill='both', expand=True, padx=26, pady=20)

        tk.Label(container, text='MTS Link OneClick', bg='#F4F7FB', fg='#15243B',
                 font=('Segoe UI', 24, 'bold')).pack(anchor='w')
        tk.Label(container, text='Показанные материалы + звук. Веб-камер в итоговом видео нет.',
                 bg='#F4F7FB', fg='#486079', font=('Segoe UI', 11)).pack(anchor='w', pady=(3, 20))

        card = tk.Frame(container, bg='white', highlightbackground='#DCE4EE', highlightthickness=1)
        card.pack(fill='x', pady=(0, 14))
        interior = tk.Frame(card, bg='white')
        interior.pack(fill='x', padx=18, pady=15)

        tk.Label(interior, text='Ссылка на запись МТС Линк', bg='white', fg='#1A2A42',
                 font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 7))
        r1 = tk.Frame(interior, bg='white')
        r1.pack(fill='x')
        self.url_entry = ttk.Entry(r1, textvariable=self.url, font=('Segoe UI', 11))
        self.url_entry.pack(side='left', fill='x', expand=True)
        ttk.Button(r1, text='Вставить', style='Secondary.TButton', command=self.paste_url).pack(side='left', padx=(9, 0))
        tk.Label(interior, text='Поддерживаются ссылки …/record-new/ID и …/record-new/ID/record-file/ID.',
                 bg='white', fg='#6B7F96', font=('Segoe UI', 9)).pack(anchor='w', pady=(6, 12))
        tk.Label(interior, text='Куда сохранять', bg='white', fg='#1A2A42',
                 font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(2, 7))
        r2 = tk.Frame(interior, bg='white')
        r2.pack(fill='x')
        ttk.Entry(r2, textvariable=self.out, font=('Segoe UI', 10)).pack(side='left', fill='x', expand=True)
        ttk.Button(r2, text='Папка…', style='Secondary.TButton', command=self.choose_folder).pack(side='left', padx=(9, 0))

        controls = tk.Frame(container, bg='#F4F7FB')
        controls.pack(fill='x', pady=(0, 13))
        self.start_btn = ttk.Button(controls, text='Скачать MP4', style='Primary.TButton', command=self.start)
        self.start_btn.pack(side='left')
        self.stop_btn = ttk.Button(controls, text='Остановить', style='Secondary.TButton', command=self.stop, state='disabled')
        self.stop_btn.pack(side='left', padx=(10, 0))
        self.folder_btn = ttk.Button(controls, text='Открыть результат', style='Secondary.TButton', command=self.open_folder, state='disabled')
        self.folder_btn.pack(side='right')

        tk.Label(container, textvariable=self.status, bg='#F4F7FB', fg='#31475F',
                 font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(0, 7))
        self.progress = ttk.Progressbar(container, mode='indeterminate')
        self.progress.pack(fill='x', pady=(0, 12))

        tk.Label(container, text='Ход работы', bg='#F4F7FB', fg='#1A2A42',
                 font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(4, 7))
        logs = tk.Frame(container, bg='white', highlightbackground='#DCE4EE', highlightthickness=1)
        logs.pack(fill='both', expand=True)
        self.log = tk.Text(logs, font=('Consolas', 9), bg='white', fg='#23384F',
                           relief='flat', wrap='word', padx=12, pady=12, state='disabled')
        bar = ttk.Scrollbar(logs, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        self.log.pack(side='left', fill='both', expand=True)
        tk.Label(container, text='Повторный запуск использует кэш. При отсутствии демонстрации программа не подменяет её веб-камерами.',
                 bg='#F4F7FB', fg='#6B7F96', font=('Segoe UI', 9)).pack(anchor='w', pady=(11, 0))
        self.url_entry.focus_set()

    def paste_url(self):
        try:
            self.url.set(self.root.clipboard_get().strip())
        except tk.TclError:
            messagebox.showinfo('Буфер обмена', 'Скопируй ссылку МТС Линк и повтори.')

    def choose_folder(self):
        if self.running:
            return
        chosen = filedialog.askdirectory(initialdir=self.out.get(), title='Папка для записей')
        if chosen:
            self.out.set(chosen)

    def append_log(self, message):
        self.log.configure(state='normal')
        self.log.insert('end', str(message) + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')
        clean = str(message).strip()
        if clean and not clean.startswith('  '):
            self.status.set(clean[:115])

    def start(self):
        if self.running:
            return
        try:
            engine.parse_recording_url(self.url.get())
        except ValueError as exc:
            messagebox.showerror('Неверная ссылка', str(exc))
            return
        out = Path(self.out.get()).expanduser()
        if not out.is_dir():
            messagebox.showerror('Папка не найдена', 'Выбери существующую папку для сохранения.')
            return
        self.running = True
        self.result = None
        self.stop_event.clear()
        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.folder_btn.configure(state='disabled')
        self.progress.start(13)
        self.append_log('--- Новая запись ---')
        link = self.url.get()
        self.worker = threading.Thread(target=self._worker, args=(link, str(out)), daemon=True)
        self.worker.start()

    def _worker(self, link, output):
        try:
            path = engine.run_recording(link, output, callback=lambda txt: self.events.put(('line', txt)),
                                        stop_event=self.stop_event)
            self.events.put(('done', str(path)))
        except engine.Cancelled:
            self.events.put(('stopped', 'Остановлено. Готовые временные файлы сохранены.'))
        except Exception as exc:
            self.events.put(('failed', str(exc)))

    def stop(self):
        if self.running:
            self.append_log('Запрошена остановка…')
            self.stop_btn.configure(state='disabled')
            self.stop_event.set()

    def process_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'line':
                    self.append_log(value)
                    continue
                self.running = False
                self.progress.stop()
                self.start_btn.configure(state='normal')
                self.stop_btn.configure(state='disabled')
                if kind == 'done':
                    self.result = Path(value)
                    self.folder_btn.configure(state='normal')
                    self.append_log('УСПЕХ: ' + value)
                    messagebox.showinfo('Готово!', 'MP4 создан. Открой его и проверь показанные материалы и звук.')
                elif kind == 'stopped':
                    self.append_log(value)
                else:
                    self.append_log('ОШИБКА: ' + value)
                    messagebox.showerror('Не удалось скачать запись',
                                         value[-1200:] + '\n\nПодробности в журнале приложения и MTS_RESULT_REPORT.txt, если он создан.')
        except queue.Empty:
            pass
        self.root.after(100, self.process_events)

    def open_folder(self):
        if not self.result:
            return
        folder = self.result.parent
        try:
            if os.name == 'nt':
                os.startfile(str(folder))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(folder)])
            else:
                subprocess.Popen(['xdg-open', str(folder)])
        except Exception as exc:
            messagebox.showerror('Не удалось открыть папку', str(exc))

    def on_close(self):
        if self.running:
            messagebox.showinfo('Идёт загрузка', 'Сначала нажми «Остановить», затем закрой программу.')
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    App().run()
