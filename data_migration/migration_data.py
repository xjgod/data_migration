# coding=utf-8

from settings_parser import *
import concurrent.futures
import json

import multiprocessing
import subprocess
import threading
import time
from concurrent.futures import as_completed
from tqdm import tqdm
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
def get_wr_name(source_db, target_db):
    reader_name = f"{'postgresql' if source_db == 'gauss' else source_db}reader"
    writer_name = f"{'postgresql' if target_db == 'gauss' else target_db}writer"
    return reader_name, writer_name


# 创建日志文件
def clear_error_log_path(error_files):
    # 创建一个 job_path 目录，如果不存在的话则创建
    if not os.path.exists(job_path):
        os.makedirs(job_path)

    # 创建一个 datax_logs 目录，如果不存在的话则创建
    if not os.path.exists(datax_logs):
        os.makedirs(datax_logs)

    # 创建一个 error_path 目录，如果不存在的话则创建
    if not os.path.exists(error_path):
        os.makedirs(error_path)

    # 初始化日志文件
    # 定义 error_file 文件路径,如果不存在则创建一个空文件，如果存在则清空
    with open(error_tables_jobs, "w", encoding="utf-8"):
        pass


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
    _, _, database, schema, user, _ = get_db_config(types)  # 获取数据库配置信息
    db_schema = 'dbo' if db_type == 'sqlserver' and schema == '' else (user if schema == '' else schema)
    try:
        # 获取数据库连接和游标对象
        conn, cursor = connect_db(db_type, types)
        if db_type == 'sqlserver':
            cursor.execute(
                f"SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = '{db_schema}' "
                f"AND TABLE_CATALOG='{database}'")
            all_tables = [table[0] for table in cursor.fetchall()]

        elif db_type == 'oracle':
            cursor.execute(
                f"SELECT table_name FROM all_tables WHERE owner = '{user}'")
            all_tables = [table[0] for table in cursor.fetchall()]

        elif db_type == 'gauss':
            cursor.execute(
                f"SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
                f"AND table_catalog = '{database}' AND table_schema = '{db_schema}'")
            all_tables = [table[0] for table in cursor.fetchall()]

        else:
            raise ValueError("不支持的数据库类型。")
    except Exception as e:
        logging.error(f"获取数据库表名失败：{e}")
        raise e
    finally:  # 关闭数据库连接和游标对象
        close_connection_and_cursor(conn, cursor)

    return all_tables


# 获取表的所有字段
def get_table_columns(db_type, types, table):
    _, _, database, schema, user, _ = get_db_config(types)
    db_schema = 'dbo' if db_type == 'sqlserver' and schema == '' else (user if schema == '' else schema)
    try:
        # 获取数据库连接和游标对象
        conn, cursor = connect_db(db_type, types)
        # 根据数据库类型，组装查询语句
        if db_type == 'sqlserver':
            cursor.execute(
                f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '{table}'"
                f"AND TABLE_CATALOG = '{database}' AND TABLE_SCHEMA = '{db_schema}'")
            table_columns = [column[0] for column in cursor.fetchall()]

        elif db_type == 'oracle':
            cursor.execute(
                f"SELECT column_name FROM all_tab_columns WHERE owner = '{user}' "
                f"AND table_name = '{table}'")
            table_columns = [column[0] for column in cursor.fetchall()]

        elif db_type == 'gauss':
            cursor.execute(
                f"SELECT column_name FROM information_schema.columns WHERE table_catalog = '{database}'"
                f" AND table_schema = '{db_schema}' AND table_name = '{table}'")
            table_columns = [column[0] for column in cursor.fetchall()]
            if not table_columns:
                logging.warning(f"{table}表字段为空")
    except Exception as e:
        logging.error(f"获取源数据库表字段失败：{e}")  # 使用logging.error()方法记录错误信息
        raise e
    finally:  # 关闭数据库连接和游标对象
        close_connection_and_cursor(conn, cursor)

    # 返回所有字段列表
    return table_columns


# 读取migration_table_file.txt文件,获取所有的表名和分片字段，并与数据库进行比对分析，返回匹配后的表与分片信息
def get_tables_and_split():
    tables = []
    split_columns = []
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
                tables = ["*"]
                split_columns = [""]
                break
            else:
                if ":" in line:
                    table, split_column = line.split(":", 1)
                else:
                    table = line
                    split_column = ""
                tables.append(table)
                split_columns.append(split_column)
    # 获取所有表名并同数据库源与目标进行匹配，返回匹配后的表与分片字段
    match_tables, split_columns, source_tables, target_tables = get_match_tables(tables, split_columns)
    # 将所有匹配成功的表matched_tables写入文件
    with open(f'{matched_table_file}', "w", encoding="utf-8") as f:
        for matched_table in match_tables:
            # 分析源库与目标库将不存在的表输出到日志文件
            f.write(f"{matched_table} \n")
    # 返回匹配表，分片信息，源表名，目标表名
    return match_tables, split_columns, source_tables, target_tables


# 读取 migration_table_file.txt 文件所有表名，与数据库进行比较，返回匹配后的表、分片字段，源表名，目标表名
def get_match_tables(tables, split_columns):
    # 获取源数据库的所有表名
    source_tables = get_all_tables(source_db, source)
    # 获取目标数据库的所有表名
    target_tables = get_all_tables(target_db, target)

    # 判断source_tables和target_tables是否为空，如果为空直接返回
    if not source_tables or not target_tables:
        logging.warning("请检查源库或目标库是否为空!")
        return

    # 如果配置为* 则取源库与目标库所有表的并集转换为大写后进行分析
    if "*" in tables:
        matched_tables = list(set([i.upper() for i in source_tables]) | set([i.upper() for i in target_tables]))
        matched_split_columns = [""] * len(matched_tables)
    else:
        matched_tables = tables
        matched_split_columns = split_columns

    # 将源库与目标库不存的表名分别保存到列表中
    source_missing_table = [tab for tab in matched_tables if tab.upper() not in [t.upper() for t in source_tables]]
    target_missing_table = [tab for tab in matched_tables if tab.upper() not in [t.upper() for t in target_tables]]

    # 如果列表不为空则分析将不存在表输出日志文件，并在mached列表中删除
    if source_missing_table or target_missing_table:
        # 分析源库与目标库将不存在的表输出到日志文件
        with open(no_match_tables, "w", encoding="utf-8") as f:
            logging.warning(f"源库中的未匹配的表：{','.join(source_missing_table)} 请检查与核对！")
            f.write(f"源库中的未匹配的表：{','.join(source_missing_table)} 请检查与核对！\n")
            logging.warning(f"目标库中的未匹配的表：{','.join(target_missing_table)} 请检查与核对！")
            f.write(f"目标库中的未匹配的表：{','.join(target_missing_table)} 请检查与核对！\n")
        # 在mached列表中删除不存在的表信息
        for smt_name in source_missing_table:
            index = matched_tables.index(smt_name)
            del matched_tables[index]
            del matched_split_columns[index]
        for tmt_name in list(set(target_missing_table) - set(source_missing_table)):
            index = matched_tables.index(tmt_name)
            del matched_tables[index]
            del matched_split_columns[index]

    # 按matched_tables列表顺序，对源库与目标库的表进行重新筛选与重排
    matched_source_tables = []
    matched_target_tables = []
    for table_name in matched_tables:
        s_table_name = [tab for tab in source_tables if tab.upper() == table_name.upper()]
        matched_source_tables = matched_source_tables + s_table_name
        t_table_name = [tab for tab in target_tables if tab.upper() == table_name.upper()]
        matched_target_tables = matched_target_tables + t_table_name

    return matched_tables, matched_split_columns, matched_source_tables, matched_target_tables


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
                       sourceUrl, targetUrl, split_column, del_mode, source_schema, target_schema):
    _, _, sourceDatabase, _, sourceUser, sourcePwd = get_db_config(source)
    _, _, targetDatabase, _, targetUser, targetPwd = get_db_config(target)
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
                            "username": sourceUser,
                            "password": sourcePwd,
                            "column": source_columns,
                            "splitPk": split_column,
                            "connection": [
                                {
                                    "table": [f"{source_schema}{source_table_name}"],
                                    "jdbcUrl": [
                                        sourceUrl
                                    ]
                                }
                            ]
                        }
                    },
                    "writer": {
                        "name": writer_name,
                        "parameter": {
                            "username": targetUser,
                            "password": targetPwd,
                            "column": target_columns,
                            "preSql": [
                                f"{del_mode} {target_schema}{target_table_name}"
                            ],
                            "connection": [
                                {
                                    "jdbcUrl": targetUrl,
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


# 保存已生成的 json 文件
def save_json_file(json_data, source_table_name):
    json_data = json.dumps(json_data, indent=4)  # 格式化json
    file_name = os.path.join(job_path, f"{source_table_name}.json")
    with open(file_name, "w", encoding="utf-8") as f:
        f.write(json_data)


# 定义一个函数，用于根据数据库类型转义字段名
def escape_column(table_columns, db_type):
    # Oracle或者gauss，就用双引号（"）
    if db_type == 'oracle' or db_type == 'gauss':
        return '"' + table_columns + '"'
    # SQL Server，就用方括号（[ ]）
    elif db_type == 'sqlserver':
        return '[' + table_columns + ']'
    else:
        return table_columns


# 生成单个表的json文件
def generate_table_json(source_table_name, target_table_name, split_column, ):
    # 获取所有字段
    source_columns = get_table_columns(source_db, source, source_table_name)
    target_columns = get_table_columns(target_db, target, target_table_name)

    # 判断 source_table_name 中的表是否存在于 special_table 中，如果存在 'DELETE FROM'，否则为 'TRUNCATE TABLE'
    if source_table_name.upper() in (table.upper() for table in special_tables):
        del_mode = 'DELETE FROM'
    else:
        del_mode = 'TRUNCATE TABLE'

    # 判断 split_column 是否在表字段中
    if split_column and split_column.upper() not in (col.upper() for col in source_columns):
        raise ValueError(f"源库表： {source_table_name} 中不存在分片字段:{split_column} 请检查！")
        # 将 split_column 置空
        split_column = ""

    # 判断源库和目标库、源表和目标表是否相同
    if source_db == target_db:
        if source_table_name == target_table_name:
            raise ValueError("不能将数据从同一个库的同一张表迁移到自己，请选择不同的库或表作为源和目标，请检查后再试！")

    # 比较源库和目标库的字段信息是否一致，并统计不一致的字段信息
    source_miss_col = [col for col in source_columns if col.lower() not in [c.lower() for c in target_columns]]
    target_miss_col = [col for col in target_columns if col.lower() not in [c.lower() for c in source_columns]]
    source_miss_col_len = len(source_miss_col)
    target_miss_col_len = len(target_miss_col)

    # 如果列表不为空则分析将不存在列输出日志文件
    if source_miss_col or target_miss_col:
        # 分析源库与目标库将不存在的表字段输出到日志文件
        with open(no_match_columns, "w", encoding="utf-8") as f:
            if source_miss_col:
                logging.warning(f"源库：{source_table_name}表有{source_miss_col_len}个字段:{','.join(source_miss_col)}"
                                f"在目标库中未存在，请检查核对！")
                f.write(f"<数据迁移> 源库：{source_table_name}表中有{source_miss_col_len}个字段:{','.join(source_miss_col)}"
                        f"在目标库中未存在，请检查核对！\n")
                # 按照字段少的表的字段进行截取
                source_columns = [col for col in source_columns if col not in source_miss_col]

            if target_miss_col:
                logging.warning(f"目标库：{target_table_name}表有{target_miss_col_len}个字段:{','.join(target_miss_col)}"
                                f"在源库中未存在，请检查核对！")
                f.write(f"<数据迁移> 目标库：{target_table_name}表有{target_miss_col_len}个字段:{','.join(target_miss_col)}"
                        f"在源库中未存在，请检查核对！\n")
                # 按照字段少的表的字段进行截取
                target_columns = [col for col in target_columns if col not in target_miss_col]

    # 创建一个字典，以目标表的字段为键，以原始大小写为值
    target_dict = {col.lower(): col for col in target_columns}
    # 把目标表的字段按照源表的字段顺序进行排序,并且考虑大小写问题
    target_columns = [target_dict.get(col.lower()) for col in source_columns if col.lower() in target_dict]

    # 把sql字段进行转义
    source_columns = [escape_column(col, source_db) for col in source_columns]
    target_columns = [escape_column(col, target_db) for col in target_columns]
    # 获取reader和writer名称
    reader_name, writer_name = get_wr_name(source_db, target_db)
    # 获取配置文件
    source_host, source_port, source_database, _, _, _ = get_db_config(source)
    target_host, target_port, target_database, _, _, _ = get_db_config(target)
    # 生成源库和目标库的url
    sourceUrl = get_build_url(source_db, source_host, source_port, source_database)
    targetUrl = get_build_url(target_db, target_host, target_port, target_database)

    source_schema = get_schema(source)
    target_schema = get_schema(target)
    # 生成json格式字典
    data = generate_json_dict(source_table_name, target_table_name, source_columns, target_columns, reader_name,
                              writer_name, sourceUrl, targetUrl, split_column, del_mode, source_schema, target_schema)
    # 生成json文件并保存到指定路径
    save_json_file(data, source_table_name)
    return 1, []


# 生成json
def generate_all_table_json(matched_tables, matched_split_columns, matched_source_tables, matched_target_tables):
    logging.info("请稍等，正在开始生成json脚本...")
    successful_table = 0  # 记录成功生成json文件的表名
    filed_tables = []  # 记录字段不匹配的表名

    # 统计进度信息
    total_pbar = tqdm(total=len(matched_tables), desc="总进度", leave=False,
                      bar_format='{desc}: {percentage:.0f}%|{bar}| {n_fmt}/{total_fmt}' + '-预计在<{remaining}>后完成  ')

    for source_table_name, target_table_name, split_column in zip(matched_source_tables, matched_target_tables,
                                                                  matched_split_columns):

        # 生成单个表的json迁移文件
        successful_tables = generate_table_json(source_table_name, target_table_name, split_column)
        if successful_tables[0] == 1:  # 如果返回1则表示生成json文件成功
            successful_table += 1  # 记录成功生成json文件的表名
        else:
            successful_table += 0
            filed_tables.append(successful_tables[1])  # 记录字段不匹配的表名
        total_pbar.update(1)  # 更新总进度条

    total_pbar.close()  # 关闭总进度条
    logging.info(f"总共生成了{successful_table}张表的JSON脚本文件，耗时{total_pbar.format_dict['elapsed']}s")
    return matched_tables


# 定义一个数据迁移类
class MigrationData:
    def __init__(self):
        self.task_finished = None
        self.exec_time = 0
        # 创建一个Manager对象
        manager = multiprocessing.Manager()
        # 创建一个共享的命名空间
        self.namespace = manager.Namespace()
        # 定义self.lock变量
        self.lock = manager.Lock()
        # 把这四个变量放在命名空间里
        self.namespace.waiting = 0
        self.namespace.running = 0
        self.namespace.succeed = 0
        self.namespace.failed = 0

    # 根据表名找到所有jobs对应的json执行脚本
    def find_job_files(self, job_path, matched_tables, error_jobs):
        result = {}

        # 循环遍历表名，构造json文件的路径，并存入结果字典中
        for table in matched_tables:
            # 使用os.path.join来拼接路径，并转换为字符串
            json_file = str(os.path.join(job_path, f"{table}.json"))
            # 检查json文件是否存在
            if not os.path.isfile(json_file):
                # 如果不存在，则输出提示信息并记录 errorjob 日志
                logging.error(f"{table.ljust(50)}#表json不存在，请检查jobs目录下是否有该文件！")
                with open(error_jobs, 'a', encoding='utf-8') as f:
                    f.write(f"{table.ljust(50)}#表json不存在，请检查jobs目录下是否有该文件！\n")
                continue
            result[table] = json_file
        self.namespace.waiting = len(result)  # 设置待执行任务数
        return result

    def run_datax(self, job, log_file):  # 定义一个执行datax的函数
        command = rf'python  {datax_path}  {job} --jvm  "{jvm_config}"'
        with open(log_file, "w") as f:  # 将日志输出到指定的日志文件
            execjob = subprocess.Popen(command, shell=True, stdout=f, stderr=f)
        with self.lock:  # 控制任务数
            self.namespace.waiting -= 1
            self.namespace.running += 1
        returncode = execjob.wait()  # 等待任务执行完
        if returncode != 0:
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
    def asyncexecjob(self, task_queue, error_jobs):
        failed_tables = set()  # 存放执行失败的表名
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
                        # 迁移失败，则输出提示信息并记录 errorjob 日志
                        logging.error(f"{table.ljust(50)}#任务迁移失败，请检查{datax_logs}目录中对应的日志文件！")
                        with open(error_jobs, 'a', encoding='utf-8') as f:
                            f.write(f"{table.ljust(50)}#任务迁移失败，请检查{datax_logs}目录中对应的日志文件！\n")
                # 使用BaseException来捕获所有类型的异常
                except BaseException as e:
                    failed_tables.add(table)
                    logging.error(f"{table.ljust(50)}#任务异常，错误信息：{e}！请检查{datax_logs}目录中对应的日志文件！")
                    with open(error_jobs, 'a', encoding='utf-8') as f:
                        f.write(f"{table.ljust(50)}#任务异常，错误信息：{e}！ 请检查{datax_logs}目录中对应的日志文件！\n")

    # 创建进程池，调用asyncexecjob执行任务
    def execJobs(self, jobpath, matched_tables):
        logging.info('迁移开始')
        start_time = time.time()  # 记录开始执行的时间

        # 获取所有待执行的作业文件
        jobs = self.find_job_files(jobpath, matched_tables, error_tables_jobs)

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
                    p_executor.submit(self.asyncexecjob, sub_queue, error_tables_jobs) for sub_queue in
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
    def taskDetails(self):
        if (self.namespace.waiting == 0 and self.namespace.running == 0
                and self.namespace.succeed == 0 and self.namespace.failed == 0):
            logging.info("正在获取任务...")
        else:
            logging.info(f"等待执行任务：{self.namespace.waiting}，正在执行任务：{self.namespace.running}，"
                         f"执行成功任务：{self.namespace.succeed}，执行失败任务：{self.namespace.failed}")

    def monitorTask(self):
        self.task_finished = False  # 初始化任务状态
        while True:
            with self.lock:
                self.taskDetails()
            time.sleep(5)  # 每5秒监测一次
            if self.namespace.waiting == 0 and self.namespace.running == 0:
                self.task_finished = True
                break
        if not self.task_finished:
            pass


if __name__ == '__main__':
    print("╔═════════════════════════════════════════════════════════════")
    print("║                      DATAX数据迁移脚本                        ")
    print("║                                               版本：V1.2.0   ")
    print("╠═════════════════════════════════════════════════════════════")
    print("║  1.生成json文件并执行数据迁移（*回车默认*）                        ")
    print("║  2.生成json文件                                               ")
    print("║  3.执行数据迁移                                               ")
    print("║  4.执行数据比对                                                ")
    print("║  9.退      出                                                ")
    print("╚═════════════════════════════════════════════════════════════")
    input_number = 0
    while True:
        input_num = input("请输入你的选择：")
        if input_num == "1" or input_num == "":
            multiprocessing.freeze_support()
            manager = multiprocessing.Manager()
            # 日志文件与目录初始化
            clear_error_log_path(error_tables_jobs)
            # 读取migration_table_file.txt文件,获取所有的表名和分片字段，并分析数据库是否存在对应表，并返回匹配的表与分版本字段
            matched_tables, matched_split_columns, matched_source_tables, matched_target_tables = get_tables_and_split()
            # 生成json
            generate_all_table_json(matched_tables, matched_split_columns, matched_source_tables, matched_target_tables)
            # 执行迁移
            migrate = MigrationData()  # 创建迁移实例
            t = threading.Thread(target=migrate.monitorTask)  # 监控任务执行情况
            t.daemon = False
            t.start()
            migrate.execJobs(job_path, matched_tables)
            exit()
        elif input_num == "2":
            # 日志文件与目录初始化
            clear_error_log_path(error_tables_jobs)
            # 读取migration_table_file.txt文件,获取所有的表名和分片字段，并分析数据库是否存在对应表，并返回匹配的表与分版本字段
            matched_tables, matched_split_columns, matched_source_tables, matched_target_tables = get_tables_and_split()
            # 生成json
            generate_all_table_json(matched_tables, matched_split_columns, matched_source_tables, matched_target_tables)
            exit()
        elif input_num == "3":
            # 表示只迁移数据，不生成json文件
            multiprocessing.freeze_support()  # 防止windows下运行报错
            manager = multiprocessing.Manager()
            # lock = manager.Lock()
            # 日志文件与目录初始化
            clear_error_log_path(error_tables_jobs)
            # 读取migration_table_file.txt文件,获取所有的表名和分片字段，并分析数据库是否存在对应表，并返回匹配的表与分版本字段
            matched_tables, matched_split_columns, matched_source_tables, matched_target_tables = get_tables_and_split()
            # 执行迁移
            migrate = MigrationData()  # 创建迁移实例
            t = threading.Thread(target=migrate.monitorTask)  # 监控任务执行情况
            t.daemon = False
            t.start()
            migrate.execJobs(job_path, matched_tables)
            exit()
        elif input_num == "4":
            from dataCompare import DataCompare

            multiprocessing.set_start_method('spawn')  # 防止linux启动异常
            dc = DataCompare()
            dc.get_hash_result()

            exit()
        elif input_num == "9" or input_num == "":
            # 表示退出程序
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
