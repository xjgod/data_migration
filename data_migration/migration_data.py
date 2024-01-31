# coding=utf-8

from settings_parser import *
import concurrent.futures
import json

import multiprocessing
import subprocess
import threading
import time
from concurrent.futures import as_completed, ThreadPoolExecutor
import jaydebeapi


# 获取相应数据库配置
def get_db_config(types):
    host = config[f'{types}_db']['host']
    port = config[f'{types}_db']['port']
    database = config[f'{types}_db']['database']
    schema = config[f'{types}_db']['schema']
    user = config[f'{types}_db']['user']
    password = config[f'{types}_db']['password']
    return host, port, database, schema, user, password


# 获取reader和writer的名称
def get_wr_name(source_db_name, target_db_name):
    reader_name = f"{'postgresql' if source_db_name == 'gauss' else source_db_name}reader"
    writer_name = f"{'postgresql' if target_db_name == 'gauss' else target_db_name}writer"
    return reader_name, writer_name


def check_db():
    # 判断源库和目标库、源表和目标表是否相同
    if source_db == target_db:
        source_schema = get_schema(source_db)
        target_schema = get_schema(target_db)
        if source_schema == target_schema:
            logging.warning("不能将数据从同一个库的同一张表迁移到自己，请选择不同的库或表作为源和目标，请检查后再试！")
            exit()


# 创建日志文件
def file_is_exist(file_path):
    for path in file_path:
        # 创建一个 job_path 目录，如果不存在的话则创建
        if not os.path.exists(path):
            os.makedirs(path)


# 构造url
def get_build_url(db_type, *args):
    if db_type == 'oracle':
        url = f"jdbc:oracle:thin:@{args[0]}:{args[1]}:{args[2]}"
    elif db_type == 'sqlserver':
        url = f"jdbc:sqlserver://{args[0]}:{args[1]};DatabaseName={args[2]};"
    elif db_type == 'gauss':
        url = f"jdbc:postgresql://{args[0]}:{args[1]}/{args[2]}"
    else:
        raise ValueError(f"暂不支持的数据库类型: {db_type}")
    return url


# 获取数据库连接和游标对象
def connect_db(db_type, types):
    host, port, database, schema, user, password = get_db_config(types)
    try:
        driver_name = f"{db_type}_driver"
        driver = config['db_driver'][driver_name]
        url = get_build_url(db_type, host, port, database)
        conn = jaydebeapi.connect(driver, url, [user, password], jar_files)
        cursor = conn.cursor()
        return conn, cursor
    except Exception as e:
        logging.error(f"{db_type}连接数据库和获取游标失败，请检查网络后再试！：{e}")
        raise e


# 关闭数据库连接和游标对象
def close_connection_and_cursor(conn, cursor):
    try:
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"关闭数据库连接和游标对象失败：{e}")
        raise e


# 获取数据库的所有表名
def get_all_tables(db_type, types):
    conn, cursor = None, None
    _, _, database, schema, user, _ = get_db_config(types)  # 获取数据库配置信息
    db_schema = 'dbo' if db_type == 'sqlserver' and schema == '' else (user if schema == '' else schema)
    try:
        # 获取数据库连接和游标对象
        conn, cursor = connect_db(db_type, types)
        if db_type == 'sqlserver':
            cursor.execute(
                f"SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = '{db_schema}' "
                f"AND TABLE_CATALOG='{database}'")
            all_tables = [table[0].upper() for table in cursor.fetchall()]

        elif db_type == 'oracle':
            cursor.execute(
                f"SELECT table_name FROM all_tables WHERE owner = '{user}'")
            all_tables = [table[0].upper() for table in cursor.fetchall()]

        elif db_type == 'gauss':
            cursor.execute(
                f"SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
                f"AND table_catalog = '{database}' AND table_schema = '{db_schema}'")
            all_tables = [table[0].upper() for table in cursor.fetchall()]
        else:
            raise ValueError("不支持的数据库类型。")
    except Exception as e:
        logging.error(f"获取数据库表名失败：{e}")
        raise e
    finally:  # 关闭数据库连接和游标对象
        close_connection_and_cursor(conn, cursor)

    return all_tables


# 获取表的所有字段
def get_all_columns(db_type, types, tables):
    conn, cursor = None, None
    table_columns = {}
    try:
        _, _, database, schema, user, _ = get_db_config(types)
        db_schema = 'dbo' if db_type == 'sqlserver' and schema == '' else (user if schema == '' else schema)
        conn, cursor = connect_db(db_type, types)
        for table, split_column in tables.items():
            table = table.lower() if db_type == 'gauss' else table.upper()
            if db_type == 'oracle':
                query = (f"SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS WHERE  OWNER = '{user}' "
                         f" AND TABLE_NAME = '{table}'")
            else:
                query = (
                    f"SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_CATALOG = '{database}' "
                    f" AND TABLE_SCHEMA = '{db_schema}' AND TABLE_NAME = '{table}'")
            cursor.execute(query)
            # 获取字段及类型
            columns = [(column[0], column[1]) for column in cursor.fetchall()]
            # 根据columns的第一个字段的大小写决定 table_case 的大小写
            table_case = 'upper' if columns[0][0].isupper() else 'lower'
            columns = [(col[0].upper(), col[1]) for col in columns]
            # 判断分片字段是否存在于columns
            if split_column:
                with open(no_match_columns, 'w', encoding='utf-8') as f:
                    if split_column.upper() not in [col[0].upper() for col in columns]:
                        logging.warning(f"表：{table}中的分片字段：{split_column}不存在，请检查！")
                        f.write(f"表：{table}中的分片字段：{split_column}不存在，请检查！\n")
                        split_column = ''
            # 移除指定数据类型的字段
            if exclude_column_type:
                excluded_data_types = [e.upper() for e in exclude_column_type]
                columns = [col[0] for col in columns if col[1].upper() not in excluded_data_types]
            table_columns[table.upper()] = {'columns': columns, 'table_case': table_case, 'split_column': split_column}
    except Exception as e:
        logging.error(f"获取表字段时发生异常: {e}")
        return {}
    finally:
        if conn and cursor:
            close_connection_and_cursor(conn, cursor)

    return table_columns


# 将字段顺序按照源表排序
def get_compare_columns(tables):
    try:
        logging.info("请稍等，正在获取数据库表等相关信息...")
        source_columns_dict = get_all_columns(source_db, source, tables)
        target_columns_dict = get_all_columns(target_db, target, tables)

        table_column_dict = {}  # 存储表名及字段相关

        with open(no_match_columns, 'a', encoding='utf-8') as f:
            for table in tables:
                source_table_info = source_columns_dict.get(table, {})
                target_table_info = target_columns_dict.get(table, {})

                # 获取字段信息
                if source_table_info and target_table_info:
                    s_columns = [col for col in source_table_info['columns']]
                    t_columns = [col for col in target_table_info['columns']]
                    # 获取缺失的字段
                    missing_in_source = [col for col in t_columns if col not in s_columns]
                    missing_in_target = [col for col in s_columns if col not in t_columns]

                    # 当目标库缺少源库的字段时，从源库字段列表中删除这些字段
                    if missing_in_target:
                        s_columns = [col for col in s_columns if col not in missing_in_target]
                        logging.warning(f"源库表：{table}中的字段：{missing_in_target}在目标库中不存在，请检查！")
                        f.write(f"源库表：{table}中的字段：{missing_in_target}在目标库中不存在，请检查！\n")

                    # 当源库缺少目标库的字段时，从目标库字段列表中删除这些字段
                    if missing_in_source:
                        t_columns = [col for col in t_columns if col not in missing_in_source]
                        logging.warning(f"目标库表：{table}中的字段：{missing_in_source}在源库中不存在，请检查！")
                        f.write(f"目标库表：{table}中的字段：{missing_in_source}在源库中不存在，请检查！\n")

                    if s_columns and t_columns:
                        del_mode = 'DELETE FROM' if table in [tb.upper() for tb in special_tables] else 'TRUNCATE TABLE'
                        # 对目标表字段进行排序，以匹配源表字段的顺序
                        t_columns.sort(key=s_columns.index)
                        table_column_dict[table] = {
                            'source_columns': s_columns,
                            'target_columns': t_columns,
                            'source_table_case': source_table_info['table_case'],
                            'target_table_case': target_table_info['table_case'],
                            'split_column': source_table_info['split_column'],
                            'del_mode': del_mode
                        }
                    else:
                        logging.warning(f"表：{table}中的字段与目标库表中的字段为空，请检查！")
                        f.write(f"表：{table}中的字段与目标库表中的字段为空，请检查！\n")
        return table_column_dict
    except Exception as e:
        logging.error(f"在比较字段时发生异常: {e}")
        return None


# 读取 migration_tables.txt 获取表相关信息
def get_tables_and_split():
    tables_dict = {}
    # 判断migration_table_file文件存不存在，不存在则创建
    if not os.path.isfile(migration_table_file):
        with open(migration_table_file, "w", encoding="utf-8") as f:
            f.write("*")
    with open(migration_table_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 读取一行记录，如为空或者以//、#开头则跳过
            if not line or line.startswith("//") or line.startswith("#"):
                continue
            # 如果为 * 则表示全部，结束循环，表示全部
            if line == "*":
                tables_dict["*"] = ""
                break
            else:
                if ":" in line:
                    table, split_column = line.split(":", 1)
                    tables_dict[table.upper()] = split_column.upper()
                else:
                    table = line
                    tables_dict[table] = ""
    # 获取所有匹配后的表信息
    match_tables_dict = get_match_tables(tables_dict)
    # 将所有匹配成功的表matched_tables写入文件
    with open(matched_table_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(match_tables_dict, indent=4))

    return match_tables_dict


# 获取比较后的表
def get_compare_tables():
    with open(no_match_tables, 'w', encoding='utf-8') as f:
        match_tables_dict = {}
        # 获取数据库的所有表名
        source_tables = get_all_tables(source_db, source)
        target_tables = get_all_tables(target_db, target)

        # 判断source_tables和target_tables是否为空，如果为空直接返回
        if not source_tables or not target_tables:
            logging.warning("请检查源库或目标库是否为空!")
            return
        # 获取两边都存在的表
        match_tables = set(source_tables).intersection(set(target_tables))
        # 获取只存在于源表中的表
        just_source_tables = set(source_tables).difference(set(target_tables))
        # 获取只存在于目标表中的表
        just_target_tables = set(target_tables).difference(set(source_tables))

        if just_source_tables:
            logging.warning(
                f"表：{just_source_tables}在目标库中不存在，请检查{migration_table_file}文件中的表填写是否正确！")
            f.write(f"表：{just_source_tables}在目标库中不存在，请检查{migration_table_file}文件中的表填写是否正确！\n")
        if just_target_tables:
            logging.warning(
                f"表：{just_target_tables}在源库中不存在，请检查{migration_table_file}文件中的表填写是否正确！")
            f.write(f"表：{just_target_tables}在源库中不存在，请检查{migration_table_file}文件中的表填写是否正确！\n")
        # 将所有分片字段置为空
        for table in match_tables:
            match_tables_dict[table] = ''

    return match_tables_dict


# 获取执行失败的表
def get_error_tables():
    with open(exec_error_tables, 'r', encoding='utf-8') as f:
        error_tables = []
        for line in f:
            line = line.strip()
            error_tables.append(line)
    return error_tables


# 获取在后的表及字段相关信息
def get_match_tables(tables_dict):
    tables = get_compare_tables()
    to_delete = []  # 存储需删除的表名
    with open(no_match_tables, "a", encoding="utf-8") as f:
        if "*" in tables_dict:
            table_columns_dict = get_compare_columns(tables)
        else:
            for table in tables_dict:
                # 判断表名是否在数据库中存在
                if table.upper() not in tables:
                    logging.warning(f"表：{table}在源库或目标库中不存在，请检查{migration_table_file}！中的表填写是否正确")
                    f.write(f"表：{table}在源库或目标库中不存在，请检查{migration_table_file}！中的表填写是否正确\n")
                    to_delete.append(table)
            # 从字典中删除不存在的表
            for table in to_delete:
                del tables_dict[table]
            table_columns_dict = get_compare_columns(tables_dict)

    return table_columns_dict


# 获取数据块模式
def get_schema(db_type):
    _, _, database, schema, user, _ = get_db_config(db_type)
    if db_type == 'sqlserver':
        db_schema = 'dbo' if schema == '' else schema
        dbs_schema = f"{database}.{db_schema}."
    elif db_type == 'oracle':
        dbs_schema = user if schema == '' else schema
    else:
        db_schema = user if schema == '' else schema
        dbs_schema = f"{database}.{db_schema}."

    return dbs_schema


# 生成json格式字典
def generate_json_dict(source_table_name, target_table_name, source_columns, target_columns, reader_name, writer_name,
                       source_url, target_url, split_column, del_mode, source_schema, target_schema, source_config, target_config):
    json_dict = {
        "job": {
            "setting": {
                "speed": {
                    "channel": 10,
                    "record": -1,
                    "byte": -1
                },
                "errorLimit": {
                    "record": 0
                }
            },
            "content": [
                {
                    "reader": {
                        "name": reader_name,
                        "parameter": {
                            "username": source_config[4],
                            "password": source_config[5],
                            "column": source_columns,
                            "splitPk": split_column,
                            "connection": [
                                {
                                    "table": [f"{source_schema}{source_table_name}"],
                                    "jdbcUrl": [
                                        source_url
                                    ]
                                }
                            ]
                        }
                    },
                    "writer": {
                        "name": writer_name,
                        "parameter": {
                            "username": target_config[4],
                            "password": target_config[5],
                            "column": target_columns,
                            "preSql": [
                                f"{del_mode} {target_schema}{target_table_name}"
                            ],
                            "connection": [
                                {
                                    "jdbcUrl": target_url,
                                    "table": [f"{target_schema}{target_table_name}"]
                                }
                            ]
                        }
                    }
                }
            ]
        }
    }

    return json_dict


# 保存生成的 json 文件
def save_json_file(json_data, source_table_name):
    job_data = json.dumps(json_data, indent=4)
    file_name = os.path.join(job_path, f"{source_table_name}.json")
    with open(file_name, "w", encoding="utf-8") as f:
        f.write(job_data)


# 根据数据库类型转义字段名
def escape_column(table_columns, db_type):
    if db_type == 'oracle' or db_type == 'gauss':
        return '"' + table_columns + '"'
    elif db_type == 'sqlserver':
        return '[' + table_columns + ']'
    else:
        return table_columns


# 生成单个表的json文件
def generate_table_json(table, table_info, reader_name, writer_name, source_url, target_url, source_schema,
                        target_schema):
    source_table_name = table.lower() if table_info['source_table_case'] == 'lower' else table.upper()
    source_columns = [col.lower() if table_info['source_table_case'] == 'lower' else col.upper() for col in
                      table_info['source_columns']]
    target_table_name = table.lower() if table_info['target_table_case'] == 'lower' else table.upper()
    target_columns = [col.lower() if table_info['target_table_case'] == 'lower' else col.upper() for col in
                      table_info['target_columns']]
    split_column = table_info['split_column']
    del_mode = table_info['del_mode']

    if not source_columns or not target_columns:
        logging.warning(f"表：{table}中的字段与目标库表中的字段或为空，请检查！")
        return 0, table

    s_escape_columns = [escape_column(col, source_db) for col in source_columns]
    t_escape_columns = [escape_column(col, target_db) for col in target_columns]

    source_config = get_db_config(source)
    target_config = get_db_config(target)
    data = generate_json_dict(source_table_name, target_table_name, s_escape_columns, t_escape_columns, reader_name,
                              writer_name, source_url, target_url, split_column, del_mode, source_schema, target_schema,
                              source_config, target_config)

    save_json_file(data, source_table_name)
    return 1, table


# 生成json
def generate_all_table_json(match_tables_dict):
    logging.info("正在开始生成json脚本...")
    # 获取reader和writer名称
    reader_name, writer_name = get_wr_name(source_db, target_db)

    # 获取源库和目标库的配置信息
    source_host, source_port, source_database, _, _, _ = get_db_config(source)
    target_host, target_port, target_database, _, _, _ = get_db_config(target)
    source_url = get_build_url(source_db, source_host, source_port, source_database)
    target_url = get_build_url(target_db, target_host, target_port, target_database)

    # 获取源库和目标库的schema
    source_schema = get_schema(source)
    target_schema = get_schema(target)

    successful_table = 0
    filed_tables = []

    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        future_to_table = {executor.submit(generate_table_json, table, match_tables_dict[table], reader_name,
                                           writer_name, source_url, target_url, source_schema,
                                           target_schema): table for table in match_tables_dict}

        for future in as_completed(future_to_table):
            table = future_to_table[future]
            try:
                result, error_table = future.result()
                if result == 1:
                    successful_table += 1
                else:
                    filed_tables.append(error_table)
            except Exception as e:
                if table not in filed_tables:
                    logging.error(f"表：{table}处理时出错：{e}")
                    filed_tables.append(table)

    logging.info(f"总共生成了{successful_table}张表的JSON迁移文件")


# 定义一个数据迁移类
class MigrationData:
    def __init__(self):
        self.task_finished = None
        self.exec_time = 0
        # 创建一个Manager对象
        manages = multiprocessing.Manager()
        # 创建一个共享的命名空间
        self.namespace = manages.Namespace()
        # 定义self.lock变量
        self.lock = manages.Lock()
        # 把这四个变量放在命名空间里
        self.namespace.waiting = 0
        self.namespace.running = 0
        self.namespace.succeed = 0
        self.namespace.failed = 0

    # 根据表名找到所有jobs对应的json执行脚本
    def find_job_files(self, matched_tables):
        result = {}
        with open(error_tables_jobs, 'w', encoding='utf-8') as f:
            for table in matched_tables:
                json_file = str(os.path.join(job_path, f"{table}.json"))
                # 检查json文件是否存在
                if not os.path.isfile(json_file):
                    # 如果不存在，则输出提示信息并记录在 error_tables_jobs 日志中
                    logging.error(f"{table.ljust(50)}#表json不存在，请检查jobs目录下是否有该文件！")
                    f.write(f"{table.ljust(50)}#表json不存在，请检查jobs目录下是否有该文件！\n")
                    continue
                result[table] = json_file
        self.namespace.waiting = len(result)  # 设置待执行任务数
        return result

    def run_datax(self, job, log_file):  # 定义一个执行datax的函数
        command = rf'python  {datax_path}  {job} --jvm  "{jvm_config}"'
        with open(log_file, "w") as f:  # 将日志输出到指定的日志文件
            exec_job = subprocess.Popen(command, shell=True, stdout=f, stderr=f)
        with self.lock:  # 控制任务数
            self.namespace.waiting -= 1
            self.namespace.running += 1
        return_code = exec_job.wait()  # 等待任务执行完
        if return_code != 0:
            logging.error(f"迁移任务：{job}执行失败")
            with self.lock:  # 控制任务数
                self.namespace.failed += 1
                self.namespace.running -= 1
            return False
        else:
            with self.lock:
                self.namespace.running -= 1
                self.namespace.succeed += 1
            logging.info(f"迁移任务：{job}执行成功")
            return True

    # 定义一个异步执行任务的函数
    def async_exec_job(self, task_queue):
        #  创建一个最大线程数为 max_threads 的线程池，调用run_datax执行任务
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as t_executor:
            li_futures = []  # 创建一个空的 li_futures 列表
            for table, job in task_queue:  # 迭代任务队列，为每个表名和作业文件提交一个任务
                log_file = os.path.abspath(os.path.join(datax_logs, f"{table}.log"))  # 用于构造日志文件的路径
                # 提交任务，并将返回的 f_threads 对象添加到 li_futures 列表中
                f_threads = t_executor.submit(self.run_datax, job, log_file)
                li_futures.append((table, f_threads))

            # 循环遍历 li_futures 列表，等待每个任务完成，并处理输出和异常
            for table, f_threads in li_futures:
                try:
                    if not f_threads.result():
                        # 迁移失败，则输出提示信息并记录 error_job 日志
                        logging.error(f"{table.ljust(50)}#任务迁移失败，请检查{datax_logs}目录中对应的日志文件！")
                        with open(error_tables_jobs, 'a', encoding='utf-8') as f:
                            f.write(f"{table.ljust(50)}#任务迁移失败，请检查{datax_logs}目录中对应的日志文件！\n")
                # 使用BaseException来捕获所有类型的异常
                except BaseException as e:
                    with open(exec_error_tables, 'a', encoding='utf-8') as f:
                        f.write(f"{table}\n")
                    logging.error(f"{table.ljust(50)}#任务异常，错误信息：{e}！请检查{datax_logs}目录中对应的日志文件！")
                    with open(error_tables_jobs, 'a', encoding='utf-8') as f:
                        f.write(f"{table.ljust(50)}#任务异常，错误信息：{e}！ 请检查{datax_logs}目录中对应的日志文件！\n")

    # 创建进程池，调用async_exec_job执行任务
    def exec_jobs(self, matched_tables):
        logging.info('迁移开始')
        start_time = time.time()  # 记录开始执行的时间

        # 获取所有待执行的作业文件
        jobs = self.find_job_files(matched_tables)

        # 创建一个空的任务队列，将表名和作业文件添加到任务队列中
        task_queue = []
        for table, job in jobs.items():
            task_queue.append((table, job))
        task_count = len(task_queue)  # 任务总数

        # 根据任务总数和最大进程数，计算出实际的进程数
        if max_processes is None or max_processes < 1:
            num_processes = min(4, task_count)
        else:
            num_processes = min(max_processes, task_count)

        if num_processes > 0:
            # 创建一个空的子任务队列列表，将任务队列中的任务均匀分配到各个子队列中
            sub_task_queues = [[] for _ in range(num_processes)]
            for index, task in enumerate(task_queue):
                sub_queue_index = index % num_processes
                sub_task_queues[sub_queue_index].append(task)

            # 使用 ProcessPoolExecutor 创建进程池，指定 max_workers 参数为 num_processes
            with concurrent.futures.ProcessPoolExecutor(max_workers=num_processes) as p_executor:
                f_processes = [
                    p_executor.submit(self.async_exec_job, sub_queue) for sub_queue in
                    sub_task_queues]  # 提交任务
                # 循环遍历 f_processes 列表，等待每个任务完成，并处理输出和异常
                for future in as_completed(f_processes):
                    try:
                        future.result()
                    except Exception as e:
                        logging.error(str(e))
        logging.info(
            f"迁移完成，总任务数：{task_count}，执行成功任务：{self.namespace.succeed}，执行失败任务：{self.namespace.failed}")
        logging.info(f"迁移结束,迁移总耗时{round(time.time() - start_time, 2)}秒")
        with open(error_tables_jobs, 'a', encoding='utf-8') as f:
            f.write(
                f"迁移完成，总任务数：{task_count}，执行成功任务：{self.namespace.succeed}，执行失败任务：{self.namespace.failed} \n")
            f.write(f"迁移结束,迁移总耗时{round(time.time() - start_time, 2)}秒 \n")

    # 打印任务执行情况
    def task_details(self):
        if (self.namespace.waiting == 0 and self.namespace.running == 0
                and self.namespace.succeed == 0 and self.namespace.failed == 0):
            logging.info("正在获取任务...")
        else:
            logging.info(f"等待执行任务：{self.namespace.waiting}，正在执行任务：{self.namespace.running}，"
                         f"执行成功任务：{self.namespace.succeed}，执行失败任务：{self.namespace.failed}")

    def monitor_task(self):
        self.task_finished = False  # 初始化任务状态
        while True:
            with self.lock:
                self.task_details()
            time.sleep(5)  # 每5秒监测一次
            if self.namespace.waiting == 0 and self.namespace.running == 0:
                self.task_finished = True
                break


if __name__ == '__main__':
    print("╔═════════════════════════════════════════════════════════════")
    print("║                      DATAX数据迁移脚本                        ")
    print("║                                               版本：V1.2.0   ")
    print("╠═════════════════════════════════════════════════════════════")
    print("║  1.生成json文件并执行数据迁移（*回车默认*）                        ")
    print("║  2.生 成 json 文 件                                           ")
    print("║  3.执 行 数 据 迁 移                                           ")
    print("║  4.执行迁移 失败 的表                                          ")
    print("║  5.执 行 数 据 比 对                                           ")
    print("║  9.退           出                                           ")
    print("╚═════════════════════════════════════════════════════════════")
    input_number = 0
    while True:
        input_num = input("请输入你的选择：")
        if input_num == "1" or input_num == "":  # 生成json文件并执行数据迁移
            check_db()
            # 目录初始化
            file_is_exist([job_path, datax_logs, error_path])
            # 获取所有表相关信息
            matched_tables_dict = get_tables_and_split()
            # 生成json
            generate_all_table_json(matched_tables_dict)
            # 执行迁移
            multiprocessing.freeze_support()
            manager = multiprocessing.Manager()
            migrate = MigrationData()  # 创建迁移实例
            t = threading.Thread(target=migrate.monitor_task)  # 监控任务执行情况
            t.daemon = False
            t.start()
            migrate.exec_jobs(matched_tables_dict)
            exit()
        elif input_num == "2":  # 只生成json文件，不迁移数据
            check_db()
            # 目录初始化
            file_is_exist([job_path, datax_logs, error_path])
            matched_tables_dict = get_tables_and_split()
            # 生成json
            generate_all_table_json(matched_tables_dict)
            exit()
        elif input_num == "3":  # 只迁移数据，不生成json文件
            # 目录初始化
            file_is_exist([job_path, datax_logs, error_path])
            matched_tables_dict = get_tables_and_split()
            # 执行迁移
            multiprocessing.freeze_support()
            manager = multiprocessing.Manager()
            migrate = MigrationData()  # 创建迁移实例
            t = threading.Thread(target=migrate.monitor_task)  # 监控任务执行情况
            t.daemon = False
            t.start()
            migrate.exec_jobs(matched_tables_dict)
            exit()
        elif input_num == "4":  # 执行迁移失败的表
            # 获取失败的表
            error_jobs = get_error_tables()
            multiprocessing.freeze_support()
            manager = multiprocessing.Manager()
            migrate = MigrationData()  # 创建迁移实例
            t = threading.Thread(target=migrate.monitor_task)  # 监控任务执行情况
            t.daemon = False
            t.start()
            migrate.exec_jobs(error_jobs)
            exit()
        elif input_num == "5":
            from dataCompare import DataCompare

            multiprocessing.set_start_method('spawn')  # 防止linux启动异常
            dc = DataCompare()
            dc.get_hash_result()
            exit()
        elif input_num == "9" or input_num == "":
            exit()
        else:
            print("输入错误，请重新输入:")
            input_number += 1
            if input_number == 3:
                print("输入错误次数超过3次，还有2次机会，请认真检查后再输入")
            if input_number == 4:
                print("输入错误次数超过4次，还有1次机会，请认真检查后再输入")
            if input_number == 5:
                print("再见！！！")
                exit()
