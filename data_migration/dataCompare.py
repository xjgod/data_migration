# -*- coding:utf-8 -*-

import concurrent.futures
import hashlib
import multiprocessing
import time

from migration_data import close_connection_and_cursor
from migration_data import connect_db
from migration_data import get_db_config
from migration_data import get_tables_and_split as tables_dict
from settings_parser import *


class DataCompare:
    def __init__(self):
        self.table_primary_keys = {}

    # 获取主键
    def get_primary_key(self, db_type, types, tables):
        conn, cursor = None, None
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
        conn, cursor = None, None
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
    def calculate_block_hash(data_block, final_hash):
        try:
            hash_value = hashlib.sha512()  # 初始化哈希对象

            if none_flag == 'True':
                for row in data_block:
                    data = [col if col is not None else '' for col in row]
                    row_str = str(data)
                    hash_value.update(row_str.encode('utf-8'))  # 更新局部哈希对象
            elif none_flag == 'False':
                begin = time.time()
                row_str = str(data_block)
                hash_value.update(row_str.encode('utf-8'))  # 更新局部哈希对象
                end = time.time()
                logging.info(f"计算数据块哈希值耗时: {end - begin}")

            final_hash.update(hash_value.digest())  # 更新到最终的哈希值
            return True
        except Exception as e:
            logging.error(f"计算数据块哈希值时发生异常: {e}")
            return False

    def calculate_table_hash(self, db_type, types, table, columns, primary_keys):
        conn, cursor = None, None
        try:
            _, _, database, schema, user, _ = get_db_config(types)
            db_schema = 'dbo' if db_type == 'sqlserver' and schema == '' else (user if schema == '' else schema)
            primary_key = ','.join(primary_keys.get(table, []))
            column_sort = ' ORDER BY ' + primary_key if primary_key else ''
            if db_type == 'oracle':
                sql_data = f"SELECT {columns} FROM {db_schema}.{table}{column_sort}"
            else:
                sql_data = f"SELECT {columns} FROM {database}.{db_schema}.{table}{column_sort}"
            conn, cursor = connect_db(db_type, types)
            cursor.execute(sql_data)
            final_hash = hashlib.sha512()
            serial = 0
            while True:
                serial += 1
                data_block = cursor.fetchmany(BLOCK_SIZE)
                if not data_block:
                    break
                if not self.calculate_block_hash(data_block, final_hash):
                    logging.error(f"处理表 {table} 时在数据块 {serial} 处发生异常")
                    with open(hash_compare_log, 'a', encoding='utf-8') as f:
                        f.write(f"处理表 {table} 时在数据块 {serial} 处发生异常\n")
                    return None

            final_hash_value = final_hash.hexdigest()
            return final_hash_value
        except Exception as e:
            logging.error(f"处理表 {table} 时发生异常: {e}")
            return None
        finally:
            if conn and cursor:
                close_connection_and_cursor(conn, cursor)

    def process_deal_tables(self, tables, source_primary_key, target_primary_key):
        future_to_table = {}  # 存储future与表名的关系
        hash_results = {}  # 存储哈希值比对结果
        begin = time.time()

        with concurrent.futures.ProcessPoolExecutor(max_workers=dc_processes) as executor:
            for table, columns_info in tables.items():
                source_columns, target_columns = columns_info.get("source_columns"), columns_info.get("target_columns")
                if source_columns is None or target_columns is None:
                    logging.warning(f"表 {table} 的列信息获取失败！")
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

    # 处理数据量不一致的表
    def deal_diff_count(self, tables):
        error_tables = []
        logging.info("正在获取表数据量，请稍后......")
        # 获取表数据量
        source_table_count = self.get_table_count(source_db, source, tables)
        target_table_count = self.get_table_count(target_db, target, tables)
        with open(f'{hash_compare_log}', 'w', encoding='utf-8') as f:
            for table in tables:
                if source_table_count[table] is not None and target_table_count[table] is not None:
                    if source_table_count[table] != target_table_count[table]:
                        error_tables.append(table)
                        logging.warning(f"{source_db}数据库中的 {table} 表与 {target_db}数据库中的 {table} 表数据量不一致！")
                        f.write(f"{source_db}数据库中的 {table} 表与 {target_db}数据库中的 {table} 表数据量不一致！\n")
                else:
                    logging.warning(f"{source_db}数据库中的 {table} 表或 {target_db}数据库中的 {table} 表记录数获取失败！")
                    f.write(f"{source_db}数据库中的 {table} 表或 {target_db}数据库中的 {table} 表记录数获取失败！\n")
        # 删除数据量不一致的表
        for table in error_tables:
            del tables[table]

        return tables

    # 获取哈希值比对任务结果
    def get_hash_result(self):
        # 获取所有表相关信息
        tables = tables_dict()
        # 处理数据量不一致的表
        match_tables = self.deal_diff_count(tables)
        # 获取表主键
        source_primary_key = self.get_primary_key(source_db, source,
                                                  tables) if source_db != 'gauss' else self.table_primary_keys
        target_primary_key = self.get_primary_key(target_db, target,
                                                  tables) if target_db != 'gauss' else self.table_primary_keys

        self.process_deal_tables(match_tables, source_primary_key, target_primary_key)


if __name__ == '__main__':
    multiprocessing.set_start_method('spawn')  # 防止linux启动异常
    dc = DataCompare()
    dc.get_hash_result()
