"""等待任务结果时，Owner 求助必须唤醒 Orchestrator，不能互相等待。"""
import threading
import pytest
from orchestration.manager import OrchestrationError, OrchestrationManager
from test_orchestration_manager import drain


def test_wait_wakes_for_help_then_completion(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id='help')
    enter = threading.Event()
    def owner(task, mgr):
        assert enter.wait(5)
        return mgr.request_owner_assistance(task.task_id, request_type='help', detail='Which source?', timeout_s=5)
    manager.register_runner('slide', owner)
    manager.submit_tasks([{'task_id': 'page', 'role': 'slide', 'pages': [1]}])
    results = []
    waiter = threading.Thread(target=lambda: results.append(manager.wait(timeout_s=5)))
    waiter.start()
    try:
        enter.set()
        waiter.join(5)
        assert not waiter.is_alive()
        request = results[0]['pending_requests'][0]
        manager.respond_owner_request(request['request_id'], {'status': 'resolved', 'resolution': 'Source A'})
        drain(manager)
        assert manager.task('page').result['resolution'] == 'Source A'
    finally:
        enter.set()
        manager.close()


def test_answered_request_cannot_resume_a_different_task(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id='help-identity')
    manager.register_runner('slide', lambda task, mgr: mgr.request_owner_assistance(
        task.task_id, request_type='help', detail='Need context', timeout_s=5))
    try:
        manager.submit_tasks([{'task_id': 'page', 'role': 'slide', 'pages': [1]}])
        request = manager.wait(timeout_s=5)['pending_requests'][0]
        response = {'status': 'resolved', 'resolution': 'Source A'}
        assert manager.respond_owner_request(request['request_id'], response)['task_id'] == 'page'
        with pytest.raises(OrchestrationError, match="already 'responded'"):
            manager.respond_owner_request(request['request_id'], response)
        with pytest.raises(OrchestrationError, match='unknown request_id'):
            manager.respond_owner_request('other-request', response)
        drain(manager)
    finally:
        manager.close()
