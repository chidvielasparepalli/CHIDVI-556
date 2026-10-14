"""Opt-in live acceptance sequence; only the isolated demo source is modified."""
import json
import time

from PyQt6.QtCore import QTimer


def start(ui, repair):
    from core.repair_permissions import _redact_event
    report = {'started_at': time.time(), 'mode': 'actual diagnostic desktop', 'cloud_used': False}
    repair.last_restart = None
    path = repair.broker.path('logs/repair/live-acceptance.json')
    state = {'stage': 'demo', 'seen_worker': False, 'deadline': time.monotonic()+90,
             'previous_failures': {item['id'] for item in repair.refresh_history()}}
    panel = ui.repair_panel
    ui.show_repair_panel()
    panel.mode.setChecked(True)
    hotkey = getattr(panel, '_native_hotkey', None)
    report['global_stop_registered'] = bool(hotkey is not None and hotkey.registered)
    panel.observation.setChecked(True)
    panel.commands['demo'].click()
    timer = QTimer(ui._win)

    def finish_report():
        if getattr(repair, 'last_restart', None):
            report['restart'] = repair.last_restart
        report['ok'] = bool(report.get('repair_verified') and report.get('capture_verified')
                            and report.get('observation_stopped')
                            and report.get('restart_requested') and report.get('restart', {}).get('ok') is True
                            and report.get('restart', {}).get('handoff', {}).get('ok'))
        path.write_text(json.dumps(_redact_event(report), indent=2), encoding='utf-8')
    ui._app.aboutToQuit.connect(finish_report)

    def tick():
        worker = repair._thread
        if worker and worker.is_alive():
            state['seen_worker'] = True
        snapshot = repair.observation.snapshot()
        if snapshot.get('latest'):
            report['real_capture'] = snapshot['latest']
        if time.monotonic() > state['deadline']:
            repair.stop()
            report['ok'] = False
            report['error'] = 'Live acceptance exceeded its deadline'
            path.write_text(json.dumps(_redact_event(report), indent=2), encoding='utf-8')
            timer.stop()
        elif state['stage'] == 'demo' and state['seen_worker'] and worker and not worker.is_alive():
            report['history'] = repair.refresh_history()
            current = [item for item in report['history'] if item['id'] not in state['previous_failures']
                       and item.get('source') == 'isolated_demo']
            report['repair_verified'] = len(current) == 1 and current[0].get('status') == 'repaired'
            report['capture_verified'] = bool(current and report.get('real_capture', {}).get('task_id') == current[0].get('task_id'))
            report['observation_before_stop'] = repair.observation.snapshot()
            repair.observation.stop()
            report['observation_stopped'] = repair.observation.snapshot()['mode'] == 'stopped'
            demo_ok = report['repair_verified'] and report['capture_verified'] and report['observation_stopped']
            QTimer.singleShot(0, lambda: panel.grab().save(str(repair.broker.path('logs/repair/live-repair-panel.png'))))
            path.write_text(json.dumps(_redact_event(report), indent=2), encoding='utf-8')
            repair.publish({'phase': 'Verified' if demo_ok else 'Blocked',
                            'message': 'Live isolated-repair check finished. Full evidence: logs/repair/live-acceptance.json'})
            if demo_ok:
                state['stage'] = 'restart'
                state['deadline'] = time.monotonic()+60
                report['restart_requested'] = True
                finish_report()
                panel.restart_button.click()
            else:
                finish_report()
                timer.stop()
        elif state['stage'] == 'restart' and worker and not worker.is_alive():
            finish_report()
            timer.stop()
    timer.timeout.connect(tick)
    timer.start(100)
    ui._repair_acceptance_timer = timer
