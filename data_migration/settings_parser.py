# -*- coding:utf-8 -*-

import configparser
import glob
import logging
import os


# 读取配置文件信息
config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')

# 数据库类型信息
source_db = config['db_type']['source_db_type']
target_db = config['db_type']['target_db_type']
databases = [source_db, target_db]

# datax参数配置信息
max_processes = int(config['other_common']['max_processes'])
max_threads = int(config['other_common']['max_threads'])
jvm_config = config['other_common']['jvm_config']
reader = config['other_common']['reader_name']
writer = config['other_common']['writer_name']
datax_path = os.path.join("../datax/bin/", "datax.py")

# 数据库连接驱动信息
jar_files = glob.glob(os.path.join('./lib', '*.jar'))

# 数据比对相关配置信息
BLOCK_SIZE = int(config['db_compare']['BLOCK_SIZE'])
none_flag = config['db_compare']['none_flag']
table_number = int(config['db_compare']['table_number'])
dc_processes = int(config['db_compare']['dc_process'])


# 文件存储路径信息
download_path = config['file_path']['download_path']
job_path = config['file_path']['job_path']
datax_logs = config['file_path']['datax_logs']
error_path = config['file_path']['error_path']
no_match_tables = os.path.join(error_path, config['file_path']['no_match_tables'])
no_match_columns = os.path.join(error_path, config['file_path']['no_match_columns'])
matched_table_file = os.path.join(error_path, config['file_path']['matched_table_file'])
error_tables_jobs = os.path.join(error_path, config['file_path']['error_tables_jobs'])
exec_error_tables = os.path.join(error_path, config['file_path']['exec_error_tables'])
migration_table_file = config['file_path']['migration_table_file']
hash_compare_log = os.path.join(error_path, config['file_path']['data_compare_log'])

# 特殊配置文件信息
special_tables = list(config['exclude_query']['special_tables'].split(','))
exclude_column_type = list(config['exclude_query']['exclude_column_type'].split(','))

# 默认配置信息
source = config['default']['sourceType']
target = config['default']['targetType']

# 设置日志的格式和级别
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

