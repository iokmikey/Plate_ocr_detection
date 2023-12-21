import cv2
import copy
import argparse
import numpy as np
import onnxruntime
from PIL import Image, ImageDraw, ImageFont

plateName = r"#京沪津渝冀晋蒙辽吉黑苏浙皖闽赣鲁豫鄂湘粤桂琼川贵云藏陕甘青宁新学警港澳挂使领民航深0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
mean_value, std_value = ((0.588, 0.193))  # 识别模型均值标准差


def decodePlate(preds):  # 识别后处理
    pre = 0
    newPreds = []
    for i in range(len(preds)):
        if preds[i] != 0 and preds[i] != pre:
            newPreds.append(preds[i])
        pre = preds[i]
    plate = ""
    for i in newPreds:
        plate += plateName[int(i)]
    return plate
    # return newPreds


def rec_pre_precessing(img, size=(48, 168)):  # 识别前处理
    img = cv2.resize(img, (168, 48))
    img = img.astype(np.float32)
    img = (img / 255 - mean_value) / std_value
    img = img.transpose(2, 0, 1)
    img = img.reshape(1, *img.shape)
    return img


def get_plate_result(img, session_rec):
    img = rec_pre_precessing(img)
    y_onnx = session_rec.run([session_rec.get_outputs()[0].name], {session_rec.get_inputs()[0].name: img})[0]
    # print(y_onnx[0])
    plate_no = decodePlate(y_onnx[0])
    return plate_no


# def allFilePath(rootPath, allFIleList):
#     fileList = os.listdir(rootPath)
#     for temp in fileList:
#         if os.path.isfile(os.path.join(rootPath, temp)):
#             allFIleList.append(os.path.join(rootPath, temp))
#         else:
#             allFilePath(os.path.join(rootPath, temp), allFIleList)


def get_split_merge(img):  # 双层车牌进行分割后识别
    h, w, c = img.shape
    img_upper = img[0:int(5 / 12 * h), :]
    img_lower = img[int(1 / 3 * h):, :]
    img_upper = cv2.resize(img_upper, (img_lower.shape[1], img_lower.shape[0]))
    new_img = np.hstack((img_upper, img_lower))
    return new_img


def order_points(pts): #对四个坐标点进行排序
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def four_point_transform(image, pts): # 对图像进行透视变换
    rect = order_points(pts)
    (tl, tr, br, bl) = rect
    widthA = np.sqrt(((br[0] - bl[0]) ** 2) + ((br[1] - bl[1]) ** 2))
    widthB = np.sqrt(((tr[0] - tl[0]) ** 2) + ((tr[1] - tl[1]) ** 2))
    maxWidth = max(int(widthA), int(widthB))
    heightA = np.sqrt(((tr[0] - br[0]) ** 2) + ((tr[1] - br[1]) ** 2))
    heightB = np.sqrt(((tl[0] - bl[0]) ** 2) + ((tl[1] - bl[1]) ** 2))
    maxHeight = max(int(heightA), int(heightB))
    dst = np.array([
        [0, 0],
        [maxWidth - 1, 0],
        [maxWidth - 1, maxHeight - 1],
        [0, maxHeight - 1]], dtype="float32")
    M = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(image, M, (maxWidth, maxHeight))

    # return the warped image
    return warped


def my_letter_box(img, size=(640, 640)):  #图像缩放并进行边界填充
    h, w, c = img.shape
    r = min(size[0] / h, size[1] / w)
    new_h, new_w = int(h * r), int(w * r)
    top = int((size[0] - new_h) / 2)
    left = int((size[1] - new_w) / 2)

    bottom = size[0] - new_h - top
    right = size[1] - new_w - left
    img_resize = cv2.resize(img, (new_w, new_h))
    img = cv2.copyMakeBorder(img_resize, top, bottom, left, right, borderType=cv2.BORDER_CONSTANT,
                             value=(114, 114, 114))
    return img, r, left, top


def xywh2xyxy(boxes): #坐标从(x, y, w, h)转换为(x1, y1, x2, y2)
    xywh = copy.deepcopy(boxes)
    xywh[:, 0] = boxes[:, 0] - boxes[:, 2] / 2
    xywh[:, 1] = boxes[:, 1] - boxes[:, 3] / 2
    xywh[:, 2] = boxes[:, 0] + boxes[:, 2] / 2
    xywh[:, 3] = boxes[:, 1] + boxes[:, 3] / 2
    return xywh


def my_nms(boxes, iou_thresh): #非极大值抑制
    index = np.argsort(boxes[:, 4])[::-1]
    keep = []
    while index.size > 0:
        i = index[0]
        keep.append(i)
        x1 = np.maximum(boxes[i, 0], boxes[index[1:], 0])
        y1 = np.maximum(boxes[i, 1], boxes[index[1:], 1])
        x2 = np.minimum(boxes[i, 2], boxes[index[1:], 2])
        y2 = np.minimum(boxes[i, 3], boxes[index[1:], 3])

        w = np.maximum(0, x2 - x1)
        h = np.maximum(0, y2 - y1)

        inter_area = w * h
        union_area = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1]) + (
                    boxes[index[1:], 2] - boxes[index[1:], 0]) * (boxes[index[1:], 3] - boxes[index[1:], 1])
        iou = inter_area / (union_area - inter_area)
        idx = np.where(iou <= iou_thresh)[0]
        index = index[idx + 1]
    return keep


def restore_box(boxes, r, left, top):  #恢复坐标到原始图像
    boxes[:, [0, 2, 5, 7, 9, 11]] -= left
    boxes[:, [1, 3, 6, 8, 10, 12]] -= top

    boxes[:, [0, 2, 5, 7, 9, 11]] /= r
    boxes[:, [1, 3, 6, 8, 10, 12]] /= r
    return boxes


def detect_pre_precessing(img, img_size):  #检测前的图像预处理
    img, r, left, top = my_letter_box(img, img_size)
    # cv2.imwrite("1.jpg",img)
    img = img[:, :, ::-1].transpose(2, 0, 1).copy().astype(np.float32)
    img = img / 255
    img = img.reshape(1, *img.shape)
    return img, r, left, top


def post_precessing(dets, r, left, top, conf_thresh=0.3, iou_thresh=0.5):  # 检测后处理
    choice = dets[:, :, 4] > conf_thresh
    dets = dets[choice]
    dets[:, 13:15] *= dets[:, 4:5]
    box = dets[:, :4]
    boxes = xywh2xyxy(box)
    score = np.max(dets[:, 13:15], axis=-1, keepdims=True)
    index = np.argmax(dets[:, 13:15], axis=-1).reshape(-1, 1)
    output = np.concatenate((boxes, score, dets[:, 5:13], index), axis=1)
    reserve_ = my_nms(output, iou_thresh)
    output = output[reserve_]
    output = restore_box(output, r, left, top)
    return output


def rec_plate(outputs, img0, session_rec):
    dict_list = []
    for output in outputs:
        result_dict = {}
        rect = output[:4].tolist()
        land_marks = output[5:13].reshape(4, 2)
        roi_img = four_point_transform(img0, land_marks)
        label = int(output[-1])
        if label == 1:  # 代表是双层车牌
            roi_img = get_split_merge(roi_img)
        plate_no = get_plate_result(roi_img, session_rec)  # 得到车牌识别结果
        result_dict['rect'] = rect
        result_dict['landmarks'] = land_marks.tolist()
        result_dict['plate_no'] = plate_no
        result_dict['roi_height'] = roi_img.shape[0]
        dict_list.append(result_dict)
    return dict_list


def cv2ImgAddText(img, text, left, top, textColor=(0, 255, 0), textSize=20):
    if (isinstance(img, np.ndarray)):  # 判断是否OpenCV图片类型
        img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img)
    fontText = ImageFont.truetype(
        "fonts/platech.ttf", textSize, encoding="utf-8")
    draw.text((left, top), text, textColor, font=fontText)
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def draw_result(orgimg, dict_list):
    result_str = ""
    for result in dict_list:
        rect_area = result['rect']

        x, y, w, h = rect_area[0], rect_area[1], rect_area[2] - rect_area[0], rect_area[3] - rect_area[1]
        padding_w = 0.05 * w
        padding_h = 0.11 * h
        rect_area[0] = max(0, int(x - padding_w))
        rect_area[1] = min(orgimg.shape[1], int(y - padding_h))
        rect_area[2] = max(0, int(rect_area[2] + padding_w))
        rect_area[3] = min(orgimg.shape[0], int(rect_area[3] + padding_h))

        height_area = result['roi_height']
        landmarks = result['landmarks']
        result = result['plate_no']
        result_str += result + " "
        for i in range(4):  # 关键点
            cv2.circle(orgimg, (int(landmarks[i][0]), int(landmarks[i][1])), 5, clors[i], -1)
        cv2.rectangle(orgimg, (rect_area[0], rect_area[1]), (rect_area[2], rect_area[3]), (0, 0, 255), 2)  # 画框
        if len(result) >= 1:
            orgimg = cv2ImgAddText(orgimg, result, rect_area[0] - height_area, rect_area[1] - height_area - 10,
                                   (255, 0, 0), height_area)
    print(result_str)  # 12312321
    return orgimg






if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--detect_model', type=str, default=r'weights/plate_detect.onnx',
                        help='model.pt path(s)')  # 检测模型
    parser.add_argument('--rec_model', type=str, default='weights/plate_rec.onnx', help='model.pt path(s)')  # 识别模型
    # parser.add_argument('--image_path', type=str, default='imgs', help='source')
    parser.add_argument('--img_size', type=int, default=640, help='inference size (pixels)')
    # parser.add_argument('--output', type=str, default='result1', help='source')
    opt = parser.parse_args()
    file_list = []
    # allFilePath(opt.image_path, file_list)
    providers = ['CPUExecutionProvider']
    clors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255)]
    img_size = (opt.img_size, opt.img_size)
    session_detect = onnxruntime.InferenceSession(opt.detect_model, providers=providers)
    session_rec = onnxruntime.InferenceSession(opt.rec_model, providers=providers)

    # 打开摄像头
    cap = cv2.VideoCapture(0)  # 0代表默认摄像头，如果有多个摄像头可以更改

    while True:
        # 逐帧捕获
        ret, frame = cap.read()
        if not ret:
            print("无法捕获图像。退出...")
            break
        img_size = (opt.img_size, opt.img_size)
        img0 = copy.deepcopy(frame)
        img, r, left, top = detect_pre_precessing(frame, img_size)
        y_onnx = session_detect.run([session_detect.get_outputs()[0].name], {session_detect.get_inputs()[0].name: img})[
            0]
        outputs = post_precessing(y_onnx, r, left, top)
        result_list = rec_plate(outputs, img0, session_rec)
        org_img = draw_result(img0, result_list)

        # 显示处理后的帧
        cv2.imshow('车牌识别', org_img)

        # 如果按下 'q' 键，退出程序
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    # 当一切都完成时，释放摄像头
    cap.release()
    cv2.destroyAllWindows()
