r"""

"""
__all__ = [
    "DataSet",
    "ApplyResultException"
]

import _pickle as pickle
from copy import deepcopy
from typing import Optional, List, Callable, Union, Dict, Any
from functools import partial
import warnings

import numpy as np
from threading import Thread

try:
    import multiprocess as mp
    from multiprocess import RLock
except:
    pass

from .field import FieldArray
from .instance import Instance
from fastNLP.core.utils.utils import pretty_table_printer, deprecated
from fastNLP.core.collators import AutoCollator
from fastNLP.core.utils.rich_progress import f_rich_progress
from fastNLP.core.collators.collator import _MultiCollator


class ApplyResultException(Exception):
    def __init__(self, msg, index=None):
        super().__init__(msg)
        self.msg = msg
        self.index = index  # 标示在哪个数据遭遇到问题了


def _apply_single(ds=None, _apply_field=None, func: Optional[Callable] = None, show_progress_bar: bool = True,
                  pipe=None, desc: str = None) -> list:
    """
    对数据集进行处理封装函数，以便多进程使用

    :param ds: 数据集
    :param _apply_field: 需要处理数据集的field_name
    :param func: 用户自定义的func
    :param pipe: 管道
    :param desc: 进度条的描述字符
    :param show_progress_bar: 是否展示子进程进度条
    :return:
    """
    if show_progress_bar:
        desc = desc if desc else f"Main"
        pg_main = f_rich_progress.add_task(description=desc, total=len(ds), visible=show_progress_bar)
    results = []
    idx = -1

    try:
        # for idx, ins in tqdm(enumerate(ds), total=len(ds), position=0, desc=desc, disable=not show_progress_bar):
        for idx, ins in enumerate(ds):
            if _apply_field is not None:
                results.append(func(ins[_apply_field]))
            else:
                results.append(func(ins))
            if pipe is not None:
                pipe.send([idx + 1])
            if show_progress_bar:
                f_rich_progress.update(pg_main, advance=1)

    except BaseException as e:
        if idx != -1:
            print("Exception happens at the `{}`th instance.".format(idx))
        raise e
    finally:
        if show_progress_bar:
            f_rich_progress.destroy_task(pg_main)
    return results


def _progress_bar(parent, total_len: int, desc: str = None, show_progress_bar: bool = True) -> None:
    """
    多进程下显示主进程的进度条

    :param parent: 进程管道
    :param total_len: 数据集总长度
    :param desc: 进度条描述符
    :param show_progress_bar: 是否展示进度条
    :return:
    """
    desc = desc if desc else "Main"

    main_pro = f_rich_progress.add_task(description=desc, total=total_len, visible=show_progress_bar)
    # pb_main = tqdm(total=total_len, desc=desc, position=0)
    nums = 0
    while True:
        msg = parent.recv()[0]
        if msg is not None:
            f_rich_progress.update(main_pro, advance=1)
            nums += 1

        if nums == total_len:
            break
    # pb_main.close()


class DataSet:
    r"""
    fastNLP的数据容器，详细的使用方法见文档  :mod:`fastNLP.core.dataset`
    """

    def __init__(self, data: Union[List[Instance], Dict[str, List[Any]], None] = None):
        r"""

        :param data: 如果为dict类型，则每个key的value应该为等长的list; 如果为list，
            每个元素应该为具有相同field的 :class:`~fastNLP.Instance` 。
        """
        self.field_arrays = {}
        self.collate_fns: _MultiCollator = _MultiCollator(AutoCollator(as_numpy=False))
        if data is not None:
            if isinstance(data, Dict):
                length_set = set()
                for key, value in data.items():
                    length_set.add(len(value))
                assert len(length_set) == 1, "Arrays must all be same length."
                for key, value in data.items():
                    self.add_field(field_name=key, fields=value)
            elif isinstance(data, List):
                for ins in data:
                    assert isinstance(ins, Instance), "Must be Instance type, not {}.".format(type(ins))
                    self.append(ins)

            else:
                raise ValueError("data only be dict or list type.")

    def __contains__(self, item):
        return item in self.field_arrays

    def __iter__(self):
        for idx in range(len(self)):
            yield self[idx]

    def _inner_iter(self):
        class Iter_ptr:
            def __init__(self, dataset, idx):
                self.dataset = dataset
                self.idx = idx

            def __getitem__(self, item):
                assert item in self.dataset.field_arrays, "no such field:{} in Instance {}".format(item, self.dataset[
                    self.idx])
                assert self.idx < len(self.dataset.field_arrays[item]), "index:{} out of range".format(self.idx)
                return self.dataset.field_arrays[item][self.idx]

            def __setitem__(self, key, value):
                raise TypeError("You cannot modify value directly.")

            def items(self):
                ins = self.dataset[self.idx]
                return ins.items()

            def __repr__(self):
                return self.dataset[self.idx].__repr__()

        def inner_iter_func():
            for idx in range(len(self)):
                yield Iter_ptr(self, idx)

        return inner_iter_func()

    def __getitem__(self, idx: Union[int, slice, str, list]):
        r"""给定int的index，返回一个Instance; 给定slice，返回包含这个slice内容的新的DataSet。

        :param idx: can be int or slice.
        :return: If `idx` is int, return an Instance object.
                If `idx` is slice, return a DataSet object.
        """
        if isinstance(idx, int):
            return Instance(**{name: self.field_arrays[name][idx] for name in self.field_arrays})
        elif isinstance(idx, slice):
            if idx.start is not None and (idx.start >= len(self) or idx.start <= -len(self)):
                raise RuntimeError(f"Start index {idx.start} out of range 0-{len(self) - 1}")
            data_set = DataSet()
            for field_name, field in self.field_arrays.items():
                data_set.add_field(field_name=field_name, fields=field.content[idx])
            return data_set
        elif isinstance(idx, str):
            if idx not in self:
                raise KeyError("No such field called {} in DataSet.".format(idx))
            return self.field_arrays[idx]
        elif isinstance(idx, list):
            dataset = DataSet()
            for i in idx:
                assert isinstance(i, int), "Only int index allowed."
                instance = self[i]
                dataset.append(instance)
            return dataset
        else:
            raise KeyError("Unrecognized type {} for idx in __getitem__ method".format(type(idx)))

    def __getattribute__(self, item):
        return object.__getattribute__(self, item)

    def __getattr__(self, item):
        # Not tested. Don't use !!
        if item == "field_arrays":
            raise AttributeError
        if isinstance(item, str) and item in self.field_arrays:
            return self.field_arrays[item]

    def __setstate__(self, state):
        self.__dict__ = state

    def __getstate__(self):
        return self.__dict__

    def __len__(self):
        r"""Fetch the length of the dataset.

        :return length:
        """
        if len(self.field_arrays) == 0:
            return 0
        field = iter(self.field_arrays.values()).__next__()
        return len(field)

    def __repr__(self):
        return str(pretty_table_printer(self))

    def append(self, instance: Instance) -> None:
        r"""
        将一个instance对象append到DataSet后面。

        :param ~fastNLP.Instance instance: 若DataSet不为空，则instance应该拥有和DataSet完全一样的field。

        """
        if len(self.field_arrays) == 0:
            # DataSet has no field yet
            for name, field in instance.items():
                # field = field.tolist() if isinstance(field, np.ndarray) else field
                self.field_arrays[name] = FieldArray(name, [field])  # 第一个样本，必须用list包装起来
        else:
            if len(self.field_arrays) != len(instance.fields):
                raise ValueError(
                    "DataSet object has {} fields, but attempt to append an Instance object with {} fields."
                        .format(len(self.field_arrays), len(instance.fields)))
            for name, field in instance.items():
                assert name in self.field_arrays
                try:
                    self.field_arrays[name].append(field)
                except Exception as e:
                    print(f"Cannot append to field:{name}.")
                    raise e

    def add_fieldarray(self, field_name: str, fieldarray: FieldArray) -> None:
        r"""
        将fieldarray添加到DataSet中.

        :param str field_name: 新加入的field的名称
        :param ~fastNLP.core.FieldArray fieldarray: 需要加入DataSet的field的内容
        :return:
        """
        if not isinstance(fieldarray, FieldArray):
            raise TypeError("Only fastNLP.FieldArray supported.")
        if len(self) != len(fieldarray):
            raise RuntimeError(f"The field to add must have the same size as dataset. "
                               f"Dataset size {len(self)} != field size {len(fieldarray)}")
        fieldarray.name = field_name
        self.field_arrays[field_name] = fieldarray

    def add_field(self, field_name: str, fields: list) -> None:
        r"""
        新增一个field， 需要注意的是fields的长度跟dataset长度一致

        :param str field_name: 新增的field的名称
        :param list fields: 需要新增的field的内容
        """

        if len(self.field_arrays) != 0:
            if len(self) != len(fields):
                raise RuntimeError(f"The field to add must have the same size as dataset. "
                                   f"Dataset size {len(self)} != field size {len(fields)}")
        self.field_arrays[field_name] = FieldArray(field_name, fields)

    def delete_instance(self, index: int):
        r"""
        删除第index个instance

        :param int index: 需要删除的instance的index，序号从0开始。
        """
        assert isinstance(index, int), "Only integer supported."
        if len(self) <= index:
            raise IndexError("{} is too large for as DataSet with {} instances.".format(index, len(self)))
        if len(self) == 1:
            self.field_arrays.clear()
        else:
            for field in self.field_arrays.values():
                field.pop(index)
        return self

    def delete_field(self, field_name: str):
        r"""
        删除名为field_name的field

        :param str field_name: 需要删除的field的名称.
        """
        if self.has_field(field_name):
            self.field_arrays.pop(field_name)
        else:
            raise KeyError(f"Field:{field_name} not found in DataSet.")
        return self

    def copy_field(self, field_name: str, new_field_name: str):
        r"""
        深度copy名为field_name的field到new_field_name

        :param str field_name: 需要copy的field。
        :param str new_field_name: copy生成的field名称
        :return: self
        """
        if not self.has_field(field_name):
            raise KeyError(f"Field:{field_name} not found in DataSet.")
        fieldarray = deepcopy(self.get_field(field_name))
        fieldarray.name = new_field_name
        self.add_fieldarray(field_name=new_field_name, fieldarray=fieldarray)
        return self

    def has_field(self, field_name: str) -> bool:
        r"""
        判断DataSet中是否有名为field_name这个field

        :param str field_name: field的名称
        :return bool: 表示是否有名为field_name这个field
        """
        if isinstance(field_name, str):
            return field_name in self.field_arrays
        return False

    def get_field(self, field_name: str) -> FieldArray:
        r"""
        获取field_name这个field

        :param str field_name: field的名称
        :return: :class:`~fastNLP.FieldArray`
        """
        if field_name not in self.field_arrays:
            raise KeyError("Field name {} not found in DataSet".format(field_name))
        return self.field_arrays[field_name]

    def get_all_fields(self) -> dict:
        r"""
        返回一个dict，key为field_name, value为对应的 :class:`~fastNLP.FieldArray`

        :return dict: 返回如上所述的字典
        """
        return self.field_arrays

    def get_field_names(self) -> list:
        r"""
        返回一个list，包含所有 field 的名字

        :return list: 返回如上所述的列表
        """
        return sorted(self.field_arrays.keys())

    def get_length(self) -> int:
        r"""
        获取DataSet的元素数量

        :return: int: DataSet中Instance的个数。
        """
        return len(self)

    def rename_field(self, field_name: str, new_field_name: str):
        r"""
        将某个field重新命名.

        :param str field_name: 原来的field名称。
        :param str new_field_name: 修改为new_name。
        """
        if field_name in self.field_arrays:
            self.field_arrays[new_field_name] = self.field_arrays.pop(field_name)
            self.field_arrays[new_field_name].name = new_field_name
        else:
            raise KeyError("DataSet has no field named {}.".format(field_name))
        return self

    def apply_field(self, func: Union[Callable], field_name: str = None,
                    new_field_name: str = None, num_proc: int = 0,
                    progress_desc: str = None, show_progress_bar: bool = True):
        r"""
        将 DataSet 中的每个 instance 中的名为 `field_name` 的 field 传给 func，并获取它的返回值。

        :param num_proc: 进程的数量
        :param field_name: 传入 func 的是哪个 field。
        :param func: input是 instance 中名为 `field_name` 的 field 的内容。
        :param new_field_name: 将 func 返回的内容放入到 `new_field_name` 这个 field 中，如果名称与已有的 field 相同，则覆
            盖之前的 field。如果为 None 则不创建新的 field。
        :param progress_desc: progress_desc 的值，默认为 Main
        :param show_progress_bar: 是否展示进度条，默认展示进度条
        """
        assert len(self) != 0, "Null DataSet cannot use apply_field()."
        if not self.has_field(field_name=field_name):
            raise KeyError("DataSet has no field named `{}`.".format(field_name))

        try:
            results = self._apply_process(num_proc=num_proc, func=func, show_progress_bar=show_progress_bar,
                                          progress_desc=progress_desc, _apply_field=field_name)
        except BaseException as e:
            raise e

        if new_field_name is not None:
            self.add_field(field_name=new_field_name, fields=results)
        return results

    def apply_field_more(self, func: Callable = None, field_name: str = None,
                         modify_fields: bool = True, num_proc: int = 0,
                         progress_desc: str = None, show_progress_bar: bool = True):
        r"""
        将 ``DataSet`` 中的每个 ``Instance`` 中的名为 `field_name` 的field 传给 func，并获取它的返回值。
        func 可以返回一个或多个 field 上的结果。

        .. note::
            ``apply_field_more`` 与 ``apply_field`` 的区别参考 :method:`~fastNLP.DataSet.apply_more` 中关于 ``apply_more`` 与
            ``apply`` 区别的介绍。

        :param num_proc: 进程的数量
        :param field_name: 传入func的是哪个field。
        :param func: 参数是 ``DataSet`` 中的 ``Instance`` ，返回值是一个字典，key 是field 的名字，value 是对应的结果
        :param modify_fields: 是否用结果修改 `DataSet` 中的 `Field`， 默认为 True
        :param show_progress_bar: 是否显示进度条，默认展示
        :param progress_desc: 当show_progress_bar为True时，可以显示当前正在处理的进度条描述字符
        :return Dict[str:Field]: 返回一个字典
        """
        assert len(self) != 0, "Null DataSet cannot use apply_field()."
        if not self.has_field(field_name=field_name):
            raise KeyError("DataSet has no field named `{}`.".format(field_name))
        idx = -1
        results = {}
        apply_out = self._apply_process(num_proc, func, progress_desc=progress_desc,
                                        show_progress_bar=show_progress_bar, _apply_field=field_name)
        #   只检测第一个数据是否为dict类型，若是则默认所有返回值为dict；否则报错。
        if not isinstance(apply_out[0], dict):
            raise Exception("The result of func is not a dict")

        for key, value in apply_out[0].items():
            results[key] = [value]
        #   尝试合并所有dict数据, idx+1 的原因是第一条数据不可能出现错误，默认第一条数据为准
        try:
            for idx, per_out in enumerate(apply_out[1:]):
                if len(set(results.keys()) - set(per_out.keys())):
                    raise ApplyResultException("apply results have different fields", idx + 1)
                for key, value in per_out.items():
                    results[key].append(value)

        except Exception as e:
            if idx != -1:
                if isinstance(e, ApplyResultException):
                    print(e.msg)
                print("Exception happens at the `{}`th instance.".format(idx + 1))
            raise e

        if modify_fields is True:
            for field, result in results.items():
                self.add_field(field_name=field, fields=result)

        return results

    def _apply_process(self, num_proc: int = 0, func: Callable = None,
                       show_progress_bar: bool = True, _apply_field: str = None,
                       progress_desc: str = 'Main') -> list:
        """
        :param num_proc: 进程的数量
        :param func: 用户自定义处理函数，参数是 ``DataSet`` 中的 ``Instance``
        :param _apply_field: 需要传进去func的数据集的field_name
        :param show_progress_bar: 是否展示progress进度条，默认为展示
        :param progress_desc: 进度条的描述字符，默认为'Main
        """

        if num_proc == 0:
            results = _apply_single(ds=self, _apply_field=_apply_field, func=func,
                                    desc=progress_desc, show_progress_bar=show_progress_bar)
        else:
            # TODO 1. desc这个需要修改一下，应该把 subprocess 的 desc 修改一下。修改成Process 1 / Process 2
            results = []
            if num_proc > len(self):
                num_proc = len(self)
                print(
                    f"num_proc must be <= {len(self)}. Reducing num_proc to {num_proc} for dataset of size {len(self)}."
                )
            #   划分数据集
            shard_len = len(self) // num_proc
            num_left_sample = len(self) % num_proc
            start = 0
            shard_data = []
            for _i in range(num_proc):
                end = shard_len + int(_i<num_left_sample) + start
                shard_data.append(self[start:end])
                start = end
            #   配置管道，线程以实现 main progress 能够实时更新。
            parent, child = mp.Pipe()
            main_thread = Thread(target=_progress_bar, args=(parent, len(self), progress_desc,
                                                             show_progress_bar))
            partial_single_map = partial(_apply_single, _apply_field=_apply_field, func=func,
                                         pipe=child, show_progress_bar=False)
            #   开启进程池，线程
            main_thread.start()
            pool = mp.Pool(processes=num_proc)
            pool_outs = [pool.apply_async(partial_single_map, kwds={'ds': ds, "proc_id": proc_id})
                         for proc_id, ds in enumerate(shard_data)]
            pool.close()
            pool.join()
            main_thread.join()

            for async_result in pool_outs:
                data = async_result.get()
                results.extend(data)
        return results

    def apply_more(self, func: Callable = None, modify_fields: bool = True,
                   num_proc: int = 0, progress_desc: str = '', show_progress_bar: bool = True):
        r"""
        将 ``DataSet`` 中每个 ``Instance`` 传入到func中，并获取它的返回值。func可以返回一个或多个 field 上的结果。

        .. note::
            ``apply_more`` 与 ``apply`` 的区别：

            1. ``apply_more`` 可以返回多个 field 的结果， ``apply`` 只可以返回一个field 的结果；

            2. ``apply_more`` 的返回值是一个字典，每个 key-value 对中的 key 表示 field 的名字，value 表示计算结果；

            3. ``apply_more`` 默认修改 ``DataSet`` 中的 field ，``apply`` 默认不修改。

        :param modify_fields: 是否用结果修改 ``DataSet`` 中的 ``Field`` ， 默认为 True
        :param func: 参数是 ``DataSet`` 中的 ``Instance`` ，返回值是一个字典，key 是field 的名字，value 是对应的结果
        :param num_proc: 进程的数量
        :param show_progress_bar:  是否使用tqd显示预处理进度
        :param progress_desc: 当show_progress_bar为True时，可以显示当前正在处理的进度条名称
        :return Dict[str:Field]: 返回一个字典
        """
        # 返回 dict , 检查是否一直相同
        assert callable(func), "The func you provide is not callable."
        assert len(self) != 0, "Null DataSet cannot use apply()."
        assert num_proc >= 0, "num_proc must >= 0"
        idx = -1

        results = {}
        apply_out = self._apply_process(num_proc, func, progress_desc=progress_desc,
                                        show_progress_bar=show_progress_bar)
        #   只检测第一个数据是否为dict类型，若是则默认所有返回值为dict；否则报错。
        if not isinstance(apply_out[0], dict):
            raise Exception("The result of func is not a dict")

        for key, value in apply_out[0].items():
            results[key] = [value]
        #   尝试合并所有dict数据, idx+1 的原因是第一条数据不可能出现错误，已经将第一条数据取出来
        try:
            for idx, per_out in enumerate(apply_out[1:]):
                if len(set(results.keys()) - set(per_out.keys())):
                    raise ApplyResultException("apply results have different fields", idx + 1)
                for key, value in per_out.items():
                    results[key].append(value)

        except Exception as e:
            if idx != -1:
                if isinstance(e, ApplyResultException):
                    print(e.msg)
                print("Exception happens at the `{}`th instance.".format(idx + 1))
            raise e

        if modify_fields is True:
            for field, result in results.items():
                self.add_field(field_name=field, fields=result)

        return results

    def apply(self, func: Callable = None, new_field_name: str = None,
              num_proc: int = 0, show_progress_bar: bool = True, progress_desc: str = ''):
        """

        :param func: 参数是 ``DataSet`` 中的 ``Instance`` ，返回值是一个字典，key 是field 的名字，value 是对应的结果
        :param new_field_name: 将func返回的内容放入到 `new_field_name` 这个field中，如果名称与已有的field相同，则覆
            盖之前的field。如果为None则不创建新的field。
        :param num_proc: 进程的数量。
        :param show_progress_bar: 是否显示进度条。
        :param progress_desc: progress bar 显示的值，默认为空。
        """
        assert callable(func), "The func you provide is not callable."
        assert len(self) != 0, "Null DataSet cannot use apply()."
        assert num_proc >= 0, "num_proc must be an integer >= 0."
        try:
            results = self._apply_process(num_proc=num_proc, func=func, show_progress_bar=show_progress_bar,
                                          progress_desc=progress_desc)
        except BaseException as e:
            raise e

        if new_field_name is not None:
            self.add_field(field_name=new_field_name, fields=results)

        return results

    def add_seq_len(self, field_name: str, new_field_name='seq_len'):
        r"""
        将使用len()直接对field_name中每个元素作用，将其结果作为sequence length, 并放入seq_len这个field。

        :param field_name: str.
        :param new_field_name: str. 新的field_name
        :return:
        """
        if self.has_field(field_name=field_name):
            self.apply_field(len, field_name, new_field_name=new_field_name)
        else:
            raise KeyError(f"Field:{field_name} not found.")
        return self

    def drop(self, func: Callable, inplace=True):
        r"""
        func接受一个Instance，返回bool值。返回值为True时，该Instance会被移除或者不会包含在返回的DataSet中。

        :param callable func: 接受一个Instance作为参数，返回bool值。为True时删除该instance
        :param bool inplace: 是否在当前DataSet中直接删除instance；如果为False，将返回一个新的DataSet。

        :return: DataSet
        """
        if inplace:
            results = [ins for ins in self if not func(ins)]
            for name, old_field in self.field_arrays.items():
                self.field_arrays[name].content = [ins[name] for ins in results]
            return self
        else:
            results = [ins for ins in self if not func(ins)]
            if len(results) != 0:
                dataset = DataSet(results)
                return dataset
            else:
                return DataSet()

    def split(self, ratio: float, shuffle=True):
        r"""
        将DataSet按照ratio的比例拆分，返回两个DataSet

        :param float ratio: 0<ratio<1, 返回的第一个DataSet拥有 `ratio` 这么多数据，第二个DataSet拥有`(1-ratio)`这么多数据
        :param bool shuffle: 在split前是否shuffle一下。为False，返回的第一个dataset就是当前dataset中前`ratio`比例的数据，
        :return: [ :class:`~fastNLP.读取后的DataSet` , :class:`~fastNLP.读取后的DataSet` ]
        """
        assert len(self) > 1, f'DataSet with {len(self)} instance cannot be split.'
        assert isinstance(ratio, float)
        assert 0 < ratio < 1
        all_indices = [_ for _ in range(len(self))]
        if shuffle:
            np.random.shuffle(all_indices)
        split = int(ratio * len(self))
        if split == 0:
            error_msg = f'Dev DataSet has {split} instance after split.'
            print(error_msg)
            raise IndexError(error_msg)
        dev_indices = all_indices[:split]
        train_indices = all_indices[split:]
        dev_set = DataSet()
        train_set = DataSet()
        for idx in dev_indices:
            dev_set.append(self[idx])
        for idx in train_indices:
            train_set.append(self[idx])

        return dev_set, train_set

    def save(self, path: str) -> None:
        r"""
        保存DataSet.

        :param str path: 将DataSet存在哪个路径
        """
        with open(path, 'wb') as f:
            pickle.dump(self, f)

    @staticmethod
    def load(path: str):
        r"""
        从保存的DataSet pickle文件的路径中读取DataSet

        :param str path: 从哪里读取DataSet
        :return: 读取后的 :class:`~fastNLP.读取后的DataSet`。
        """
        with open(path, 'rb') as f:
            d = pickle.load(f)
            assert isinstance(d, DataSet), "The object is not DataSet, but {}.".format(type(d))
        return d

    def concat(self, dataset: 'DataSet', inplace:bool=True, field_mapping:Dict=None) -> 'DataSet':
        """
        将当前dataset与输入的dataset结合成一个更大的dataset，需要保证两个dataset都包含了相同的field。结合后的dataset的input,target
            以及collate_fn以当前dataset为准。当dataset中包含的field多于当前的dataset，则多余的field会被忽略；若dataset中未包含所有
            当前dataset含有field，则会报错。

        :param DataSet, dataset: 需要和当前dataset concat的dataset
        :param bool, inplace: 是否直接将dataset组合到当前dataset中
        :param dict, field_mapping: 当传入的dataset中的field名称和当前dataset不一致时，需要通过field_mapping把输入的dataset中的
            field名称映射到当前field. field_mapping为dict类型，key为dataset中的field名称，value是需要映射成的名称

        :return: DataSet
        """
        assert isinstance(dataset, DataSet), "Can only concat two datasets."

        fns_in_this_dataset = set(self.get_field_names())
        fns_in_other_dataset = dataset.get_field_names()
        reverse_field_mapping = {}
        if field_mapping is not None:
            fns_in_other_dataset = [field_mapping.get(fn, fn) for fn in fns_in_other_dataset]
            reverse_field_mapping = {v: k for k, v in field_mapping.items()}
        fns_in_other_dataset = set(fns_in_other_dataset)
        fn_not_seen = list(fns_in_this_dataset - fns_in_other_dataset)

        if fn_not_seen:
            raise RuntimeError(f"The following fields are not provided in the dataset:{fn_not_seen}")

        if inplace:
            ds = self
        else:
            ds = deepcopy(self)

        for fn in fns_in_this_dataset:
            ds.get_field(fn).content.extend(deepcopy(dataset.get_field(reverse_field_mapping.get(fn, fn)).content))

        return ds

    @classmethod
    def from_pandas(cls, df):
        """
        从pandas.DataFrame中读取数据转为Dataset
        :param df:
        :return:
        """
        df_dict = df.to_dict(orient='list')
        return cls(df_dict)

    def to_pandas(self):
        """
        将dataset转为pandas.DataFrame类型的数据

        :return:
        """
        import pandas as pd
        dict_ = {key: value.content for key, value in self.field_arrays.items()}
        return pd.DataFrame.from_dict(dict_)

    # TODO 应该有返回值的吧
    def to_csv(self, path: str) -> None:
        """
        将dataset保存为csv文件

        :param path:
        :return:
        """

        df = self.to_pandas()
        df.to_csv(path, encoding="utf-8")

    def add_collate_fn(self, collate_fn: Callable) -> None:
        """
        添加collate_fn函数，调用该函数后会将其添加到已有的collate_fn后面

        :param collate_fn: Callable的函数
        :return:
        """
        self.collate_fns.add_collator(collate_fn)

    def set_collate_fn(self, collate_fn: Callable) -> None:
        """
        设置collate_fn函数，调用该函数后覆盖当前所有的collate_fn，包括Auto_Collate

        :param collate_fn:
        :return:
        """
        self.collate_fns = _MultiCollator(collate_fn)

    def set_pad_val(self, *field_names, val: Optional[int] = 0) -> None:
        """
        设置每个field_name的padding值，默认为0，只有当Auto_collate存在时该方法有效
        当val=None时，意味着给定的field_names都不需要尝试padding

        :param field_names: dataset存在的field_name
        :param val: 默认为0
        :return:
        """
        for field_name in field_names:
            self.collate_fns.set_pad_val(field_name, val=val)

    def set_input(self, *field_names) -> None:
        """
        被设置为inputs的field_names，会输入到AutoCollator中，未被设置默认过滤掉

        :param field_names:
        :return:
        """
        self.collate_fns.set_input(*field_names)

    def get_collator(self) -> _MultiCollator:
        """
        获取dataset绑定的collate_fn，其中包括auto_collate

        :return:
        """
        return self.collate_fns

    @deprecated()
    def set_target(self, *field_names) -> None:
        """
        被设置为inputs的field_names，会输入到AutoCollator中，未被设置默认过滤掉

        :param field_names:
        :return:
        """
        self.collate_fns.set_input(*field_names)


class IterableDataset:
    pass
