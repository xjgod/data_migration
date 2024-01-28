# -*- coding:utf-8 -*-

import concurrent.futures
import hashlib
import multiprocessing
import time

from settings_parser import *
from migration_data import get_tables_and_split
from migration_data import connect_db
from migration_data import close_connection_and_cursor
from migration_data import get_db_config


class DataCompare:
    def __init__(self):
        self.table_primary_keys = {}

    # 判断文件夹是否存在，不存在则创建
    @staticmethod
    def is_exists(path):
        if not os.path.exists(path):
            os.makedirs(path)

    # 获取需要比较的表
    @staticmethod
    def get_tables():
        tables, _, _, _ = get_tables_and_split()
        return tables

    # 获取移除指定类型后的字段
    @staticmethod
    def get_column(db_type, types, table_):
        conn = None
        cursor = None
        try:
            _, _, database, schema, user, _ = get_db_config(types)
            db_schema = 'dbo' if db_type == 'sqlserver' and schema == '' else (user if schema == '' else schema)
            conn, cursor = connect_db(db_type, types)
            table = table_.lower() if db_type == 'gauss' else table_.upper()
            if db_type == 'oracle':
                query = (f"SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS WHERE  OWNER = '{user}' "
                         f" AND TABLE_NAME = '{table}'")
            else:
                query = (
                    f"SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_CATALOG = '{database}' "
                    f" AND TABLE_SCHEMA = '{db_schema}' AND TABLE_NAME = '{table}'")
            cursor.execute(query)
            columns = [(column[0].upper(), column[1].upper()) for column in cursor.fetchall()]
            # 移除指定数据类型的字段
            for exclude_column in exclude_column_type:
                columns = [col[0] for col in columns if col[1] not in exclude_column]
            return columns
        except Exception as e:
            logging.error(f"获取表字段时发生异常: {e}")
            return []
        finally:
            if conn and cursor:
                close_connection_and_cursor(conn, cursor)

    # 将字段顺序按照源表排序
    def get_compare_columns(self, table):
        try:
            source_columns = self.get_column(source_db, source, table)
            target_columns = self.get_column(target_db, target, table)

            if not source_columns or not target_columns:
                return None, None

            with open(f'{no_match_columns}', 'w', encoding='utf-8') as f:
                if len(source_columns) > len(target_columns):
                    missing_columns = [column for column in source_columns if column not in target_columns]
                    logging.warning(
                        f"字段不一致！{target_db}数据库中的 {table} 表缺少以下字段 {','.join(missing_columns)}！")
                    f.write(f"<数据比对> 字段不一致！{target_db}数据库中的 {table} 表缺少以下字段 {','.join(missing_columns)}！\n")
                    source_columns = [column for column in source_columns if column not in missing_columns]

                elif len(source_columns) < len(target_columns):
                    missing_columns = [column for column in target_columns if column not in source_columns]
                    logging.warning(
                        f"字段不一致！{source_db}数据库中的 {table} 表缺少以下字段 {','.join(missing_columns)}！")
                    f.write(f"<数据比对> 字段不一致！{source_db}数据库中的 {table} 表缺少以下字段 {','.join(missing_columns)}！\n")
                    target_columns = [column for column in target_columns if column not in missing_columns]

                target_columns = sorted(target_columns, key=source_columns.index)
                return source_columns, target_columns
        except Exception as e:
            logging.error(f"获取表字段时发生异常: {e}")
            return None, None

    # 获取主键
    def get_primary_key(self, db_type, types, tables):
        conn = None
        cursor = None
        primary_keys_dict = {}
        try:
            _, _, _, schema, user, _ = get_db_config(types)
            db_schema = 'dbo' if db_type == 'sqlserver' and schema == '' else (user if schema == '' else schema)
            conn, cursor = connect_db(db_type, types)
            for table in tables:
                try:
                    query = (
                        f"select x.column_name from user_cons_columns x, user_constraints y "
                        f"where x.constraint_name = y.constraint_name and y.owner = '{user}' and y.constraint_type = 'P' "
                        f"and y.table_name = '{table}'"
                        if db_type == 'oracle'
                        else f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                             f"WHERE OBJECTPROPERTY(OBJECT_ID(CONSTRAINT_SCHEMA + '.' + CONSTRAINT_NAME), 'IsPrimaryKey') = 1 "
                             f"AND TABLE_NAME = '{table}' AND TABLE_SCHEMA = '{db_schema}'")

                    cursor.execute(query)
                    result = cursor.fetchall()
                    primary_key = [item[0] for item in result]
                    primary_keys_dict[table] = primary_key
                    if types == 'source':
                        self.table_primary_keys[table] = primary_key
                except Exception as e:
                    logging.warning(f"获取表 {table} 的主键时发生异常: {e}")
                    primary_keys_dict[table] = []
        except Exception as e:
            logging.error(f"获取表的主键时发生异常: {e}")
            return None
        finally:
            if conn and cursor:
                close_connection_and_cursor(conn, cursor)
        return primary_keys_dict

    @staticmethod
    def get_table_count(db_type, types, tables):
        conn = None
        cursor = None
        table_count = {}  # 存储表记录数
        try:
            _, _, database, schema, user, _ = get_db_config(types)
            db_schema = 'dbo' if db_type == 'sqlserver' and schema == '' else (user if schema == '' else schema)
            conn, cursor = connect_db(db_type, types)
            for table in tables:
                try:
                    if db_type == 'oracle':
                        query = f"SELECT COUNT(*) FROM {db_schema}.{table}"
                    else:
                        query = f"SELECT COUNT(*) FROM {database}.{db_schema}.{table}"
                    cursor.execute(query)
                    count = cursor.fetchone()[0]
                    table_count[table] = count
                except Exception as e:
                    logging.warning(f"获取表 {table} 的记录数时发生异常: {e}")
                    table_count[table] = None
            return table_count
        except Exception as e:
            logging.error(f"获取表记录数时发生异常: {e}")
            return None
        finally:
            if conn and cursor:
                close_connection_and_cursor(conn, cursor)

    @staticmethod
    def calculate_block_hash(serial, data_block):
        hash_values = []
        try:
            if none_flag == 'True':
                for row in data_block:
                    data = [col if col is not None else '' for col in row]
                    row_str = str(data)
                    hash_value = hashlib.sha512()
                    hash_value.update(row_str.encode('utf-8'))
                    hash_values.append(hash_value.hexdigest())
                # 将数据块的哈希值合并为一个哈希值
                final_hash = hashlib.sha512()
                data = str(hash_values)
                final_hash.update(data.encode('utf-8'))
                hash_values = [final_hash.hexdigest()]

            elif none_flag == 'False':
                begin = time.time()
                row_str = str(data_block)
                hash_value = hashlib.sha512()
                hash_value.update(row_str.encode('utf-8'))
                hash_values.append(hash_value.hexdigest())
                end = time.time()
                logging.info(f"计算数据块哈希值耗时: {end - begin}")
        except Exception as e:
            logging.error(f"计算第{serial}个哈希值时发生异常: {e}")
            return None
        finally:
            return hash_values

    def calculate_table_hash(self, db_type, types, table, columns, primary_keys):
        conn = None
        cursor = None
        try:
            hash_values = []
            _, _, database, schema, user, _ = get_db_config(types)
            db_schema = 'dbo' if db_type == 'sqlserver' and schema == '' else (user if schema == '' else schema)
            # 获取表主键
            primary_key = ','.join(primary_keys.get(table, []))
            column_sort = ' ORDER BY ' + primary_key if primary_key else ''
            if db_type == 'oracle':
                sql_data = f"SELECT {columns} FROM {db_schema}.{table}  {column_sort}"
            else:
                sql_data = f"SELECT {columns} FROM {database}.{db_schema}.{table}  {column_sort}"
            conn, cursor = connect_db(db_type, types)
            cursor.execute(sql_data)
            serial = 0
            while True:
                serial += 1
                begin = time.time()
                # 获取指定大小的数据
                data_block = cursor.fetchmany(BLOCK_SIZE)
                end = time.time()
                logging.info(f"获取数据块大小{BLOCK_SIZE}耗时: {end - begin}")

                if not data_block:
                    break
                # 计算数据块的哈希值
                hash_values.extend(self.calculate_block_hash(serial, data_block))

            final_hash = hashlib.sha512()
            hash_data = str(hash_values)
            # 将整张表的数据块的哈希值合并为一个哈希值
            final_hash.update(hash_data.encode('utf-8'))
            final_hash = final_hash.hexdigest()
            return final_hash
        except Exception as e:
            logging.error(f"Error: {e}")
            return None
        finally:
            if conn and cursor:
                close_connection_and_cursor(conn, cursor)

    def process_deal_tables(self, tables, source_primary_key, target_primary_key):
        future_to_table = {}  # 存储future与表名的关系
        hash_results = {}  # 存储哈希值比对结果
        begin = time.time()

        with concurrent.futures.ProcessPoolExecutor(max_workers=dc_processes) as executor:
            for table in tables:
                source_columns, target_columns = self.get_compare_columns(table)
                if source_columns is None or target_columns is None:
                    continue
                source_column_str = f"{','.join(source_columns)}"
                target_column_str = f"{','.join(target_columns)}"

                source_future = executor.submit(self.calculate_table_hash, source_db, source,
                                                table, source_column_str, source_primary_key)
                target_future = executor.submit(self.calculate_table_hash, target_db, target,
                                                table, target_column_str, target_primary_key)
                # 将future与表名关联
                future_to_table[source_future] = (table, source)
                future_to_table[target_future] = (table, target)

            for hash_future in concurrent.futures.as_completed(future_to_table):
                table, types = future_to_table[hash_future]  # 获取future对应的表名与类型
                try:
                    results = hash_future.result()  # 获取future的结果
                    if table not in hash_results:
                        hash_results[table] = {source: None, target: None}
                    hash_results[table][types] = results
                except Exception as e:
                    logging.error(f"处理{types}中的表{table} 时出错: {e}")
                    hash_results[table][types] = None
            self.write_compare_log(hash_results, begin)

    @staticmethod
    def write_compare_log(hash_results, begin):
        with open(f'{hash_compare_log}', 'a', encoding='utf-8') as f:
            for table, hash_result in hash_results.items():
                source_hash = hash_result.get(source)
                target_hash = hash_result.get(target)

                if source_hash is None or target_hash is None:
                    logging.error(f"无法获取 {table} 表的哈希值。")
                    f.write(f"无法获取 {table} 表的哈希值。\n")
                    continue

                if source_hash != target_hash:
                    logging.warning(f"{source_db}库的表 {table} 与 {target_db}库的表 {table} 数据不一致！")
                    f.write(f"{source_db}库的表 {table} 与 {target_db}库的表 {table} 数据不一致！\n")
                else:
                    logging.info(f"{source_db}库的表 {table} 与 {target_db}库的表 {table} 数据一致！")
                    f.write(f"{source_db}库的表 {table} 与 {target_db}库的表 {table} 数据一致！\n")

            end = time.time()
            logging.info(f"比对总计耗时: {end - begin}s")
            f.write(f"比对总计耗时: {end - begin}s\n")

    # 区分大表
    @staticmethod
    def get_big_tables(tables, source_table_count, target_table_count):
        big_tables = []
        for table in tables:
            if source_table_count[table] >= table_number or target_table_count[table] >= table_number:
                tables.remove(table)
                big_tables.append(table)
        return big_tables

    # 处理数据量不一致的表
    def deal_diff_count(self, tables):
        match_tables = []
        logging.info("正在获取表数据量，请稍后......")
        # 获取表数据量
        source_table_count = self.get_table_count(source_db, source, tables)
        target_table_count = self.get_table_count(target_db, target, tables)
        with open(f'{hash_compare_log}', 'w', encoding='utf-8') as f:
            for table in tables:
                if source_table_count[table] is not None and target_table_count[table] is not None:
                    if source_table_count[table] == target_table_count[table]:
                        match_tables.append(table)
                    else:
                        logging.warning(f"{source_db}数据库中的 {table} 表与 {target_db}数据库中的 {table} 表数据量不一致！")
                        f.write(f"{source_db}数据库中的 {table} 表与 {target_db}数据库中的 {table} 表数据量不一致！\n")
                else:
                    logging.warning(f"{source_db}数据库中的 {table} 表或 {target_db}数据库中的 {table} 表记录数获取失败！")
                    f.write(f"{source_db}数据库中的 {table} 表或 {target_db}数据库中的 {table} 表记录数获取失败！\n")
        return match_tables

    # 获取哈希值比对任务结果
    def get_hash_result(self):
        tables = self.get_tables()
        match_tables = self.deal_diff_count(tables)
        # 获取表主键
        source_primary_key = self.get_primary_key(source_db, source,
                                                  tables) if source_db != 'gauss' else self.table_primary_keys
        target_primary_key = self.get_primary_key(target_db, target,
                                                  tables) if target_db != 'gauss' else self.table_primary_keys

        self.process_deal_tables(match_tables, source_primary_key, target_primary_key)

        '''
        TODO:
        
        # 获取大表且将大表从tables中移除
        # big_tables = self.get_big_tables(tables, source_table_count, target_table_count)
        # with open('123.txt', 'w', encoding='utf-8') as f:
        #     f.write(f"{big_tables}\n")
        # self.process_deal_table(big_tables, source_table_count, target_table_count, source_primary_key, target_primary_key)
        '''


if __name__ == '__main__':
    multiprocessing.set_start_method('spawn')  # 防止linux启动异常
    dc = DataCompare()
    dc.get_hash_result()
