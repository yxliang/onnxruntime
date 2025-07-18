# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
from __future__ import annotations

import uuid
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto
from onnx.helper import float32_to_float8e4m3, np_dtype_to_tensor_dtype
from onnx.numpy_helper import float8e4m3_to_float32
from onnx.reference import ReferenceEvaluator
from onnx.reference import ops as onnx_ops
from onnx.reference.custom_element_types import float8e4m3fn, float8e4m3fnuz, float8e5m2, float8e5m2fnuz
from onnx.reference.op_run import OpRun

import onnxruntime
import onnxruntime.capi._pybind_state as C
from onnxruntime.quantization import CalibrationDataReader

onnx_recent_enough = hasattr(OpRun, "infer_name")

if onnx_recent_enough:
    # Test with ReferenceEvaluator requires PR https://github.com/onnx/onnx/pull/5408/.
    # https://github.com/onnx/onnx/pull/5408
    try:
        from onnx.reference.op_run import to_array_extended

    except ImportError:
        to_array_extended = None
        onnx_recent_enough = False


class QOpRun(OpRun):
    op_domain = "com.microsoft"

    f8_types = {
        TensorProto.FLOAT8E4M3FN,
        TensorProto.FLOAT8E4M3FNUZ,
        TensorProto.FLOAT8E5M2,
        TensorProto.FLOAT8E5M2FNUZ,
    }

    def get_tensor_type(self, tensor: np.ndarray) -> int:
        if tensor.dtype == float8e4m3fn and tensor.dtype.descr[0][0] == "e4m3fn":
            return TensorProto.FLOAT8E4M3FN
        if tensor.dtype == float8e4m3fnuz and tensor.dtype.descr[0][0] == "e4m3fnuz":
            return TensorProto.FLOAT8E4M3FNUZ
        if tensor.dtype == float8e5m2 and tensor.dtype.descr[0][0] == "e5m2":
            return TensorProto.FLOAT8E5M2
        if tensor.dtype == float8e5m2fnuz and tensor.dtype.descr[0][0] == "e5m2fnuz":
            return TensorProto.FLOAT8E5M2FNUZ
        return np_dtype_to_tensor_dtype(tensor.dtype)


class QGemm(QOpRun):
    def _run(
        self,
        A,
        a_scale,
        a_zero_point,
        B,
        b_scale,
        b_zero_point,
        C=None,
        y_scale=None,
        y_zero_point=None,
        transA=0,
        transB=0,
        alpha=1.0,
    ):
        if transA:
            A = A.T
        if transB:
            B = B.T

        a_type = self.get_tensor_type(a_zero_point)
        b_type = self.get_tensor_type(b_zero_point)
        y_type = self.get_tensor_type(y_zero_point)
        if a_type == TensorProto.FLOAT8E4M3FN and b_type == TensorProto.FLOAT8E4M3FN:
            a_scaled = (float8e4m3_to_float32(A).astype(float) - float8e4m3_to_float32(a_zero_point)) * np.float32(
                a_scale
            )
            b_scaled = (float8e4m3_to_float32(B).astype(float) - float8e4m3_to_float32(b_zero_point)) * np.float32(
                b_scale
            )
            y = a_scaled @ b_scaled * np.float32(alpha)
            if C is not None:
                dtype = self.get_tensor_type(C)
                if dtype not in (TensorProto.FLOAT, TensorProto.FLOAT16):
                    raise TypeError(f"C.dtype must be float16 or float 32 not {dtype}.")
                y += C.astype(np.float32)
            if y_scale is not None:
                y /= y_scale
            if y_zero_point is not None:
                y += float8e4m3_to_float32(y_zero_point)
                ry = y.ravel()

                fy = np.empty(ry.shape, dtype=float8e4m3fn)
                for i in range(fy.shape[0]):
                    el = float32_to_float8e4m3(ry[i])  # type: ignore[assignment]
                    fy[i] = el
                y = fy.reshape(y.shape)
            else:
                raise NotImplementedError("y_zero_point is not empty. QGemm is not implemented in that case.")
            return (y,)
        elif a_type in self.f8_types or b_type in self.f8_types or y_type in self.f8_types:
            raise NotImplementedError(f"QGemm not implemented for zero_types {a_type}, {b_type}, {y_type}.")
        else:
            if TensorProto.FLOAT8E4M3FN in {a_type, b_type, y_type}:
                raise TypeError(f"Unexpected type for A: {dtype}, B:{dtype} or Y:{dtype}.")
            a_scaled = (A.astype(float) - a_zero_point) * np.float32(a_scale)
            b_scaled = (B.astype(float) - b_zero_point) * np.float32(b_scale)
            y = a_scaled @ b_scaled * np.float32(alpha)
            if C is not None:
                y += C * np.float32(a_scale) * np.float32(b_scale)
            if y_scale is not None:
                y /= np.float32(y_scale)
            if y_zero_point is not None:
                y += y_zero_point

            if y_zero_point is not None:
                dtype = y_zero_point.dtype
            elif C is not None:
                dtype = C.dtype
            else:
                dtype = A.dtype

            y = np.rint(y)
            if dtype == np.uint8:
                y = np.clip(y, 0, 255)
            elif dtype == np.int8:
                y = np.clip(y, -128, 127)
            else:
                raise ValueError(f"Unexpected dtype={dtype}, it should be uint8 or int8.")

            return (y.astype(dtype),)


class QLinearMatMul(QOpRun):
    def _run(
        self,
        A,
        a_scale,
        a_zero_point,
        B,
        b_scale,
        b_zero_point,
        y_scale=None,
        y_zero_point=None,
    ):
        a_type = self.get_tensor_type(a_zero_point)
        b_type = self.get_tensor_type(b_zero_point)
        y_type = self.get_tensor_type(y_zero_point)
        if a_type == TensorProto.FLOAT8E4M3FN and b_type == TensorProto.FLOAT8E4M3FN:
            a_scaled = (float8e4m3_to_float32(A).astype(float) - float8e4m3_to_float32(a_zero_point)) * np.float32(
                a_scale
            )
            b_scaled = (float8e4m3_to_float32(B).astype(float) - float8e4m3_to_float32(b_zero_point)) * np.float32(
                b_scale
            )
            y = a_scaled @ b_scaled
            if y_scale is not None:
                y /= y_scale
            if y_zero_point is not None:
                y += float8e4m3_to_float32(y_zero_point)
                ry = y.ravel()

                fy = np.empty(ry.shape, dtype=float8e4m3fn)
                for i in range(fy.shape[0]):
                    el = float32_to_float8e4m3(ry[i])  # type: ignore[assignment]
                    fy[i] = el
                y = fy.reshape(y.shape)
            else:
                raise NotImplementedError("y_zero_point is not empty. QLinearMatMul is not implemented in that case.")
            return (y,)
        elif a_type in self.f8_types or b_type in self.f8_types or y_type in self.f8_types:
            raise NotImplementedError(f"QLinearMatMul not implemented for zero_types {a_type}, {b_type}, {y_type}.")
        else:
            if TensorProto.FLOAT8E4M3FN in {a_type, b_type, y_type}:
                raise TypeError(f"Unexpected type for A: {a_type}, B:{b_type} or Y:{y_type}.")
            a_scaled = (A.astype(float) - a_zero_point) * np.float32(a_scale)
            b_scaled = (B.astype(float) - b_zero_point) * np.float32(b_scale)
            y = a_scaled @ b_scaled
            if y_scale is not None:
                y /= np.float32(y_scale)
            if y_zero_point is not None:
                y += y_zero_point

            if y_zero_point is not None:
                dtype = y_zero_point.dtype
            elif C is not None:
                dtype = C.dtype
            else:
                dtype = A.dtype

            y = np.rint(y)
            if dtype == np.uint8:
                y = np.clip(y, 0, 255)
            elif dtype == np.int8:
                y = np.clip(y, -128, 127)
            else:
                raise ValueError(f"Unexpected dtype={dtype}, it should be uint8 or int8.")

            return (y.astype(dtype),)


class TestDataFeeds(CalibrationDataReader):
    def __init__(self, data_feeds):
        """
        parameter data_feeds: list of input feed, each input feed is diction of {input_name: np_array}
        """
        self.data_feeds = data_feeds
        self.iter_next = iter(self.data_feeds)

    def get_next(self):
        return next(self.iter_next, None)

    def rewind(self):
        self.iter_next = iter(self.data_feeds)


def input_feeds_neg_one_zero_one(n, name2shape, seed=None):
    """
    randomize n feed according to shape, its values are from -1, 0, and 1
    """
    if seed is not None:
        np.random.seed(seed)

    input_data_list = []
    for _i in range(n):
        inputs = {}
        for name, shape in name2shape.items():
            inputs.update({name: np.random.randint(-1, 2, shape).astype(np.float32)})
        input_data_list.extend([inputs])
    dr = TestDataFeeds(input_data_list)
    return dr


def input_feeds_neg_one_zero_one_list(n, name2shape, seed=None):
    """
    randomize n feed according to shape, its values are from -1, 0, and 1
    """
    if seed is not None:
        np.random.seed(seed)

    input_data_list = []
    for _i in range(n):
        inputs = {}
        for name, shape in name2shape.items():
            inputs.update({name: np.random.randint(-1, 2, shape).astype(np.float32)})
        input_data_list.extend([inputs])
    return input_data_list


class GenerateCalibrationData(CalibrationDataReader):
    def __init__(self, data_list, input_nodes, input_shapes, no_tensor_num, in_dtypes, inputs_conv_channel_last=None):
        print("Generating calibration dataset from " + str(data_list))
        print("input nodes are ", input_nodes, "input shapes are ", input_shapes)
        if inputs_conv_channel_last:
            print(f"Inputs that will be converted to channel last: {inputs_conv_channel_last}")

        self.enum_data_dicts = []
        self.input_nodes = input_nodes
        self.input_shapes = input_shapes
        self.inputs_conv_channel_last = inputs_conv_channel_last
        self.calibration_dataset = data_list

    def __len__(self):
        return len(self.calibration_dataset)

    def get_next(self):
        feed_dict = {}
        inp = next(self.calibration_dataset, None)
        if inp is not None:
            for i in range(len(self.input_nodes)):
                input_data = inp[i].reshape(self.input_shapes[i])
                if self.inputs_conv_channel_last is not None and self.input_nodes[i] in self.inputs_conv_channel_last:
                    input_data = np.moveaxis(input_data, 1, -1)
                dict_item = {self.input_nodes[i]: input_data}
                feed_dict.update(dict_item)
            return feed_dict
        else:
            return None


class StridedDataReader(GenerateCalibrationData):
    def __init__(
        self,
        data_list,
        input_nodes,
        input_shapes,
        no_tensor_num,
        in_dtypes,
        inputs_conv_channel_last=None,
        stride=1,
        start_index=0,
        end_index=None,
    ):
        super().__init__(data_list, input_nodes, input_shapes, no_tensor_num, in_dtypes, inputs_conv_channel_last)

        self.stride = max(1, stride)  # Ensure stride is at least 1
        self.start_index = start_index
        self.end_index = (
            end_index if end_index is not None else len(self.calibration_dataset)
        )  # Default to the end of the dataset
        self.enum_data_dicts = iter([])

    def get_next(self):
        iter_data = next(self.enum_data_dicts, None)
        if iter_data:
            return iter_data

        self.enum_data_dicts = None
        if self.start_index < self.end_index:
            print(f"start index is {self.start_index}")
            data = self.load_serial()

            self.start_index += self.stride
            self.enum_data_dicts = iter(data)

            return next(self.enum_data_dicts, None)
        else:
            return None

    def load_serial(self):
        batch_data = []
        end_loop = min(self.end_index, self.start_index + self.stride)
        for i in range(self.start_index, end_loop):
            print(f"debugging the load serial index {i}")
            data_item = self.calibration_dataset[i]
            processed_item = self.process_data_item(data_item)
            batch_data.append(processed_item)
        return batch_data

    def process_data_item(self, data_item):
        feed_dict = {}
        for _, node in enumerate(self.input_nodes):
            # input_data = data_item[i].reshape(self.input_shapes[i])
            feed_dict[node] = data_item["input"]
        return feed_dict

    def set_range(self, start_index, end_index=None):
        self.start_index = start_index
        self.end_index = end_index if end_index is not None else len(self.calibration_dataset)
        self.enum_data_dicts = iter([])

    def rewind(self):
        """Rewind the data reader to the beginning of the dataset."""
        self.start_index = 0
        self.enum_data_dicts = iter([])


def check_op_type_order(testcase, model_to_check, ops):
    if isinstance(model_to_check, str):
        model = onnx.load(model_to_check)
    elif isinstance(model_to_check, onnx.ModelProto):
        model = model_to_check

    testcase.assertEqual(len(ops), len(model.graph.node), "op count is not same")
    for node_idx, node in enumerate(model.graph.node):
        testcase.assertEqual(
            ops[node_idx],
            node.op_type,
            f"op {node_idx} is not in order. Expected: {ops[node_idx]}, Actual: {node.op_type}",
        )


def check_op_type_count(testcase, model_path, **kwargs):
    model = onnx.load(Path(model_path))
    optype2count = {}
    for op_type in kwargs:
        optype2count[op_type] = 0
    for node in model.graph.node:
        if node.op_type in optype2count:
            optype2count[node.op_type] += 1

    for op_type, value in kwargs.items():
        try:
            testcase.assertEqual(
                value,
                optype2count[op_type],
                f"op_type {op_type} count not same",
            )
        except AssertionError as e:
            from onnx_array_api.plotting.text_plot import onnx_simple_text_plot  # noqa: PLC0415

            raise AssertionError(
                f"Assert failed:\noptype={optype2count}\nkwargs={kwargs}\n{onnx_simple_text_plot(model)}"
            ) from e


def check_sign_f8_quantization(model_path_origin, model_path_to_check):
    """
    Quantization to float 8 type does not change the sign as zero_point is always null.
    This function checks that the quantized parameters did not change.
    """
    with open(model_path_origin, "rb") as f:
        model = onnx.load(f)
    names = {init.name: init for init in model.graph.initializer}
    with open(model_path_to_check, "rb") as f:
        model_f8 = onnx.load(f)
    names_f8 = {init.name: init for init in model_f8.graph.initializer}
    for init in model_f8.graph.initializer:
        if not init.name.endswith("_quantized"):
            continue
        name = init.name.replace("_quantized", "")
        if name not in names:
            raise AssertionError(f"Unable to find {name!r} in {set(names)}.")
        scale_zp = [i.name for i in model_f8.graph.initializer if i.name.startswith(name)]
        if len(scale_zp) not in (1, 3):
            raise AssertionError(
                f"Need one or three names not {scale_zp}, all names: { {i.name for i in model_f8.graph.initializer} }."
            )
        scale = [name for name in scale_zp if "scale" in name]
        zero = [name for name in scale_zp if "zero" in name]
        if len(scale_zp) == 3:
            if len(scale) != 1:
                raise AssertionError(f"Need one name not {scale}.")
            if len(zero) != 1:
                raise AssertionError(f"Need one name not {zero}.")
        else:
            if len(scale) != 0:
                raise AssertionError(f"No scale is expected but has {scale}.")
            if len(zero) != 0:
                raise AssertionError(f"No zero is expected but has {zero}.")

        expected_sign = onnx.numpy_helper.to_array(names[name]) >= 0

        if "bias" in init.name:
            if init.data_type >= 17:
                raise AssertionError(f"bias {init.name!r} should be float 16 not {init.data_type}.")
            continue
        if init.data_type < 17:
            raise AssertionError(f"Initializer {init.name!r} not a float 8 type.")
        raw = np.array([int(i) for i in init.raw_data])
        got_sign = raw <= 128
        try:
            np.testing.assert_allclose(expected_sign.ravel(), got_sign)
        except AssertionError as e:
            scale_value = onnx.numpy_helper.to_array(names_f8[scale[0]])
            err_msg = f"Sign are different for {name!r}, scale={scale_value}."
            if to_array_extended is not None:
                values = onnx.numpy_helper.to_array(names[name]).flatten()
                f8_values = to_array_extended(init)
                zero = onnx_ops.op_cast.Cast_19.eval(np.array(0), to=init.data_type)
                dq = onnx_ops.op_dequantize_linear.DequantizeLinear.eval(f8_values, scale_value, zero).flatten()
                q = onnx_ops.op_quantize_linear.QuantizeLinear_19.eval(values, scale_value, zero).flatten()
                qdq = onnx_ops.op_dequantize_linear.DequantizeLinear.eval(q, scale_value, zero).flatten()
                err_msg = (
                    f"{err_msg}\nvalues={values[:20]}\nqu={f8_values.flatten()[:20]}"
                    f"\n{q.flatten()[:20]}\ndq={dq[:20]}\nqdq={qdq[:20]}"
                )
            raise AssertionError(err_msg) from e


def check_model_correctness(
    testcase,
    model_path_origin,
    model_path_to_check,
    inputs,
    rtol=1e-2,
    atol=0.05,
    providers=None,
    dynamic=False,
    is_gemm=False,
    op_matmul=False,
):
    if providers is None:
        providers = ["CPUExecutionProvider"]
    sess_options = onnxruntime.SessionOptions()
    sess_options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_DISABLE_ALL
    sess_options.optimized_model_filepath = model_path_to_check + ".optimized.onnx"
    origin_sess = onnxruntime.InferenceSession(model_path_origin, sess_options=sess_options, providers=providers)
    origin_results = origin_sess.run(None, inputs)

    with open(model_path_origin, "rb") as f:
        model_onnx = onnx.load(f)
    ops_set = {node.op_type for node in model_onnx.graph.node}
    check_reference_evaluator = not (ops_set & {"EmbedLayerNormalization", "Conv", "Attention", "Transpose"})
    check_target_evaluator = False

    with open(model_path_to_check, "rb") as f:
        model_check = onnx.load(f)

    if check_reference_evaluator and onnx_recent_enough:
        ref = ReferenceEvaluator(model_path_origin)
        ref_origin_results = ref.run(None, inputs)
        for idx, ref_output in enumerate(origin_results):
            output = ref_origin_results[idx]
            np.testing.assert_allclose(
                ref_output,
                output,
                rtol=rtol,
                atol=atol,
                err_msg=f"Model {model_path_origin!r} failed for providers={providers!r}.",
            )

    # Verifies the shapes in the quantized model.
    if is_gemm:
        expected_shapes = {}
        with open(model_path_origin, "rb") as f:
            model = onnx.load(f)
            for init in model.graph.initializer:
                expected_shapes[init.name] = tuple(init.dims)
        checked = 0
        f8_quantization = False
        for init in model_check.graph.initializer:
            if init.name.endswith("_quantized"):
                name = init.name.replace("_quantized", "")
                expected = expected_shapes[name]
                shape = tuple(init.dims)
                if not dynamic and expected != shape:
                    raise AssertionError(
                        f"Shape mismatch for initializer {init.name!r} from {init.name!r}, "
                        f"shape={shape} != {expected} (expected)."
                    )
                else:
                    checked += 1
            if "zero_point" in init.name:
                dt = init.data_type
                f8_quantization = f8_quantization or dt in (
                    TensorProto.FLOAT8E4M3FN,
                    TensorProto.FLOAT8E4M3FNUZ,
                    TensorProto.FLOAT8E5M2,
                    TensorProto.FLOAT8E5M2FNUZ,
                )
        if checked == 0:
            raise AssertionError(
                f"Unable to check expected shape, expected_shapes={expected_shapes}, "
                f"names={[init.name for init in model_check.graph.initializer]}."
            )
        if f8_quantization:
            check_sign_f8_quantization(model_path_origin, model_path_to_check)

    # Verifies the expected outputs.
    if check_target_evaluator and onnx_recent_enough:
        if op_matmul:
            reference_new_ops = [QLinearMatMul]
        else:
            reference_new_ops = [QGemm]
        has_missing_reference_ops = any(
            node.domain not in ["", "ai.onnx"]
            and not any(
                node.domain == new_node.op_domain and node.op_type == new_node.__name__
                for new_node in reference_new_ops
            )
            for node in model_check.graph.node
        )
        if has_missing_reference_ops:
            # We need to skip the test if the model contains ops that are not supported.
            testcase.skipTest(
                f"Model {model_path_to_check!r} contains ops that are not supported by the reference evaluator."
            )
        # Needs pv.Version(onnx.__version__) >= pv.Version("1.16.0")
        ref = ReferenceEvaluator(model_check, new_ops=reference_new_ops)
        try:
            target_results = ref.run(None, inputs)
        except Exception as e:
            if "axis is out of boundary" not in str(e) and "list assignment index out of range" not in str(e):
                # Run through the same failure with more logs
                ref = ReferenceEvaluator(model_check, new_ops=reference_new_ops, verbose=10)
                target_results = ref.run(None, inputs)
            else:
                target_results = []
        if target_results:
            testcase.assertEqual(len(origin_results), len(target_results), "result count are different")
            for idx, ref_output in enumerate(origin_results):
                output = target_results[idx]
                np.testing.assert_allclose(
                    ref_output,
                    output,
                    rtol=rtol,
                    atol=atol,
                    err_msg=f"Model {model_path_to_check!r} failed for providers={providers!r}.",
                )

    # enable QDQ transformers
    # sess_options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
    try:
        target_sess = onnxruntime.InferenceSession(
            model_path_to_check,
            sess_options=sess_options,
            providers=providers,
        )
    except (C.Fail, C.InvalidGraph) as e:
        # This should disabled when QDQ optimizers is implemented.
        se = str(e)
        if (
            "com.microsoft:QLinearMatMul(-1) is not a registered function/op" not in se
            and "Type 'tensor(float16)' of input parameter (input) of operator (QuantizeLinear)" not in se
            and "Type 'tensor(float16)' of input parameter (input) of operator (DynamicQuantizeLinear)" not in se
        ):
            # com.microsoft:QLinearMatMul is not yet implemented.
            # QuantizeLinear supports float16 in opset 19
            raise e
        return
    target_results = target_sess.run([], inputs)
    testcase.assertEqual(len(origin_results), len(target_results), "result count are different")
    for idx, ref_output in enumerate(origin_results):
        output = target_results[idx]
        np.testing.assert_allclose(
            ref_output,
            output,
            rtol=rtol,
            atol=atol,
            err_msg=f"Model {model_path_to_check!r} failed for providers={providers!r}.",
        )


def check_op_nodes(testcase, model_path, node_checker):
    model = onnx.load(Path(model_path))
    for node in model.graph.node:
        testcase.assertTrue(node_checker(node))


def check_qtype_by_node_type(testcase, model_to_check, check_list):
    if isinstance(model_to_check, str):
        model = onnx.load(model_to_check)
    elif isinstance(model_to_check, onnx.ModelProto):
        model = model_to_check
    # NOTE: ONNX shape inference does not work on MS domain nodes.
    # Therefore, this function cannot currently be used for graphs that contain ops such as
    # com.microsoft.QuantizeLinear, which support 16-bit quantization.
    model = onnx.shape_inference.infer_shapes(model)
    value_infos = {vi.name: vi for vi in model.graph.value_info}
    value_infos.update({ot.name: ot for ot in model.graph.output})
    value_infos.update({it.name: it for it in model.graph.input})
    initializers = {init.name: init for init in model.graph.initializer}

    for node in model.graph.node:
        if node.op_type in check_list:
            input_output_check_list = check_list[node.op_type]
            for check_item in input_output_check_list:
                tensor_name = node.input[check_item[1]] if check_item[0] == "i" else node.output[check_item[1]]
                if tensor_name not in value_infos and tensor_name not in initializers:
                    raise AssertionError(
                        f"Unable to find tensor_name={tensor_name!r} in {sorted(value_infos)}\n{model}"
                    )
                if tensor_name in value_infos:
                    vi = value_infos[tensor_name]
                    testcase.assertTrue(vi.type.HasField("tensor_type"))
                    testcase.assertTrue(vi.type.tensor_type.elem_type == check_item[2])
                else:  # if (tensor_name in initializers):
                    init = initializers[tensor_name]
                    testcase.assertEqual(init.data_type, check_item[2])


def create_clip_node(input_name, output_name, node_name, initializers, min_value=-1.0, max_value=1.0):
    clip_min_name = str(uuid.uuid4())
    clip_max_name = str(uuid.uuid4())
    clip_inputs = [input_name, clip_min_name, clip_max_name]
    clip_outputs = [output_name]
    clip_name = node_name
    initializers.append(onnx.numpy_helper.from_array(np.array(min_value, dtype=np.float32), name=clip_min_name))
    initializers.append(onnx.numpy_helper.from_array(np.array(max_value, dtype=np.float32), name=clip_max_name))
    return onnx.helper.make_node("Clip", clip_inputs, clip_outputs, name=clip_name)


def generate_random_initializer(initializer_name, tensor_shape, tensor_dtype, mean=0.0, dev=0.3):
    """
    Helper function to generate initializers for test inputs
    """
    tensor = np.random.normal(mean, dev, tensor_shape).astype(tensor_dtype)
    init = onnx.numpy_helper.from_array(tensor, initializer_name)
    return init


def get_tensor_consumers_and_producers(
    model: onnx.ModelProto,
) -> tuple[dict[str, list[onnx.NodeProto]], dict[str, onnx.NodeProto]]:
    """
    Returns a tuple containing the following python dictionaries:
      - consumers: maps a tensor name to the list of nodes that have that tensor as an input.
      - producers: maps a tensor name to the node that generates this tensor as an output.
    """
    consumers: dict[str, list[onnx.NodeProto]] = {}
    producers: dict[str, onnx.NodeProto] = {}
    for node in model.graph.node:
        # Iterate through node's inputs to build the consumers dictionary.
        for input_name in node.input:
            if input_name:
                if input_name not in consumers:
                    consumers[input_name] = []

                consumers[input_name].append(node)

        # Iterate through node's outputs to build the producers dictionary.
        for output_name in node.output:
            producers[output_name] = node

    return (consumers, producers)
