#
#  Copyright 2019 The FATE Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

from fate_flow.db.db_models import Job, TaskSet, Task
from fate_flow.entity.constant import TaskStatus, EndStatus, InterruptStatus, RetCode, FederatedSchedulingStatusCode, StatusSet, OngoingStatus
from fate_flow.settings import API_VERSION, ALIGN_TASK_INPUT_DATA_PARTITION_SWITCH
from fate_flow.utils import job_utils
from fate_flow.utils.api_utils import federated_api
from fate_flow.scheduler.federated_scheduler import FederatedScheduler
from fate_flow.operation.job_saver import JobSaver
from fate_flow.scheduler.status_engine import StatusEngine
from arch.api.utils.log_utils import schedule_logger


class TaskScheduler(object):
    @classmethod
    def schedule(cls, job, task_set: TaskSet):
        schedule_logger(job_id=job.f_job_id).info("Schedule job {} task set {}".format(task_set.f_job_id, task_set.f_task_set_id))
        # TODO: Query the latest version of each task
        tasks = job_utils.query_task(job_id=job.f_job_id, task_set_id=task_set.f_task_set_id, role=task_set.f_role, party_id=task_set.f_party_id)
        for task in tasks:
            new_task_status = cls.calculate_multi_party_task_status(task=task)
            if new_task_status != task.f_status:
                task.f_status = new_task_status
                FederatedScheduler.sync_task(job=job, task=task, update_fields=["status"])
                cls.update_task_on_initiator(initiator_task_template=task, update_fields=["status"])

            if task.f_status == TaskStatus.WAITING:
                # TODO: run task until the concurrency is reached
                task.f_status = TaskStatus.START
                schedule_logger(job_id=task.f_job_id).info("Try to start job {} task {} {} on {} {}".format(task.f_job_id, task.f_task_id, task.f_task_version, task.f_role, task.f_party_id))
                update_status = JobSaver.update_task(task_info=task.to_human_model_dict(only_primary_with=["job_id", "status"]))
                if not update_status:
                    # Another scheduler scheduling the task
                    schedule_logger(job_id=task.f_job_id).info("Job {} task {} {} start on another scheduler".format(task.f_job_id, task.f_task_id, task.f_task_version))
                    continue
                schedule_logger(job_id=task.f_job_id).info("Start job {} task {} {} on {} {}".format(task.f_job_id, task.f_task_id, task.f_task_version, task.f_role, task.f_party_id))
                cls.start_task(job=job, task=task)
            elif task.f_status == TaskStatus.START:
                continue
            elif task.f_status == TaskStatus.RUNNING:
                # TODO: check the concurrency is reached
                continue
            elif InterruptStatus.contains(task.f_status):
                # The state is determined by the TaskSet as a whole
                continue
            elif task.f_status == TaskStatus.COMPLETE:
                FederatedScheduler.stop_task(job=job, task=task, stop_status=task.f_status)
            else:
                raise Exception("Job {} task set {} with a {} status cannot be scheduled".format(task_set.f_job_id, task_set.f_task_set_id, task.f_status))
        # new_task_set_status = cls.calculate_task_set_status(task_status=[task.f_status for task in tasks])
        tasks_status = [task.f_status for task in tasks]
        new_task_set_status = StatusEngine.vertical_convergence(tasks_status, interrupt_break=False)
        schedule_logger(job_id=task_set.f_job_id).info("Job {} task set {} status is {}, calculate by task status list: {}".format(task_set.f_job_id, task_set.f_task_set_id, new_task_set_status, tasks_status))
        if new_task_set_status != task_set.f_status:
            task_set.f_status = new_task_set_status
            FederatedScheduler.sync_task_set(job=job, task_set=task_set, update_fields=["status"])
            cls.update_task_set_on_initiator(initiator_task_set_template=task_set, update_fields=["status"])
        if EndStatus.contains(task_set.f_status):
            cls.finish(job=job, task_set=task_set, end_status=task_set.f_status)

    @classmethod
    def start_task(cls, job, task):
        task_parameters = {}
        #extra_task_parameters = TaskScheduler.align_task_parameters(job_id, job_parameters, job_initiator, job_args, component, task_id)
        #task_parameters.update(extra_task_parameters)
        task_parameters.update(job.f_runtime_conf["job_parameters"])
        FederatedScheduler.start_task(job=job, task=task, task_parameters=task_parameters)

    @staticmethod
    def align_task_parameters(job_id, job_parameters, job_initiator, job_args, component, task_id):
        parameters = component.get_role_parameters()
        component_name = component.get_name()
        extra_task_parameters = {'input_data_partition': 0}  # Large integers are not used
        for role, partys_parameters in parameters.items():
            for party_index in range(len(partys_parameters)):
                party_parameters = partys_parameters[party_index]
                if role in job_args:
                    party_job_args = job_args[role][party_index]['args']
                else:
                    party_job_args = {}
                dest_party_id = party_parameters.get('local', {}).get('party_id')
                if job_parameters.get('align_task_input_data_partition', ALIGN_TASK_INPUT_DATA_PARTITION_SWITCH):
                    response = federated_api(job_id=job_id,
                                             method='POST',
                                             endpoint='/{}/schedule/{}/{}/{}/{}/{}/input/args'.format(
                                                 API_VERSION,
                                                 job_id,
                                                 component_name,
                                                 task_id,
                                                 role,
                                                 dest_party_id),
                                             src_party_id=job_initiator['party_id'],
                                             dest_party_id=dest_party_id,
                                             src_role=job_initiator['role'],
                                             json_body={'job_parameters': job_parameters,
                                                        'job_args': party_job_args,
                                                        'input': component.get_input()},
                                             work_mode=job_parameters['work_mode'])
                    if response['retcode'] == 0:
                        for input_data in response.get('data', {}).get('data', {}).values():
                            for data_table_info in input_data.values():
                                if data_table_info and not isinstance(data_table_info, list):
                                    partitions = data_table_info['partitions']
                                    if extra_task_parameters['input_data_partition'] == 0 or partitions < extra_task_parameters['input_data_partition']:
                                        extra_task_parameters['input_data_partition'] = partitions
                    else:
                        raise Exception('job {} component {} align task parameters failed on {} {}'.format(job_id,
                                                                                                           component_name,
                                                                                                           role,
                                                                                                           dest_party_id))
        return extra_task_parameters

    @classmethod
    def calculate_task_set_status(cls, task_status):
        tmp_status_set = set(task_status)
        if len(tmp_status_set) == 1:
            return tmp_status_set.pop()
        else:
            for status in tmp_status_set:
                if not EndStatus.contains(status=status):
                    break
            else:
                # All tasks are end status and there are more than two different status
                for status in sorted(InterruptStatus.status_list(), key=lambda s: StatusSet.get_level(status=s), reverse=True):
                    if status in tmp_status_set:
                        return status
                raise Exception("calculate task set status failed: {}".format(task_status))
            # Not all tasks are end status
            for status in sorted(OngoingStatus.status_list(), key=lambda s: StatusSet.get_level(status=s), reverse=True):
                if status in tmp_status_set:
                    return status
            raise Exception("calculate task set status failed: {}".format(task_status))

    @classmethod
    def calculate_multi_party_task_status(cls, task):
        tasks = job_utils.query_task(task_id=task.f_task_id, task_version=task.f_task_version)
        tasks_party_status = [task.f_party_status for task in tasks]
        new_task_status = StatusEngine.horizontal_convergence(tasks_party_status)
        schedule_logger(job_id=task.f_job_id).info("Job {} task {} {} status is {}, calculate by task party status list: {}".format(task.f_job_id, task.f_task_id, task.f_task_version, new_task_status, tasks_party_status))
        return new_task_status

    @classmethod
    def update_task_on_initiator(cls, initiator_task_template: Task, update_fields: list):
        tasks = JobSaver.query_task(task_id=initiator_task_template.f_task_id, task_version=initiator_task_template.f_task_version)
        if not tasks:
            raise Exception("Failed to update task status on initiator")
        task_info = initiator_task_template.to_human_model_dict(only_primary_with=update_fields)
        for field in update_fields:
            task_info[field] = getattr(initiator_task_template, "f_%s" % field)
        for task in tasks:
            task_info["role"] = task.f_role
            task_info["party_id"] = task.f_party_id
            JobSaver.update_task(task_info=task_info)

    @classmethod
    def update_task_set_on_initiator(cls, initiator_task_set_template: TaskSet, update_fields: list):
        task_sets = JobSaver.query_task_set(job_id=initiator_task_set_template.f_job_id, task_set_id=initiator_task_set_template.f_task_set_id)
        if not task_sets:
            raise Exception("Failed to update task set status on initiator")
        task_set_info = initiator_task_set_template.to_human_model_dict(only_primary_with=update_fields)
        for field in update_fields:
            task_set_info[field] = getattr(initiator_task_set_template, "f_%s" % field)
        for task_set in task_sets:
            task_set_info["role"] = task_set.f_role
            task_set_info["party_id"] = task_set.f_party_id
            JobSaver.update_task_set(task_set_info=task_set_info)

    @classmethod
    def finish(cls, job: Job, task_set: TaskSet, end_status):
        schedule_logger(job_id=task_set.f_job_id).info("Finish job {} task set {}".format(task_set.f_job_id, task_set.f_task_set_id))
        FederatedScheduler.stop_task_set(job=job, task_set=task_set, stop_status=end_status)